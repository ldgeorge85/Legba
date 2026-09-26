# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Collection descriptor schema — a bounded, versioned HOLDING of the past
(Program 7g §2, ``planning/PROGRAM_7G_COLLECTIONS_DESIGN_2026-09-25.md``).

A ``SourceDescriptor`` is a live feed: polled on a cadence, its silence a
health signal, its arrival able to wake a reactive trigger, everything it
produces stamped ``origin_class='live'``. A :class:`CollectionDescriptor` is
the opposite kind of thing — a manifest naming exactly which series or
documents, for which subjects, over which years, from which provider, under
which licence, fetched ONCE by the operator and then left alone. A collection
has no cadence, no cooldown and no health.

That is why this is its own descriptor family beside source/analyst/
action_pack/target rather than "a source with a date range", and why the file
convention is ``descriptors/collection_<id>.yaml`` and never ``source_``: the
source machinery globs ``descriptors/source_*.yaml`` and a mis-prefixed
collection would be picked up as a live feed.

THREE THINGS THIS SCHEMA REFUSES, all fail-closed at validation:

``licence_class``
    Required, and drawn from the access-class vocabulary already on sources
    (:data:`legba.data.provenance.access.ACCESS_CLASSES`). A manifest without
    a recorded class does not parse, so it cannot load (§D.6). The
    ``licence:`` block beneath it must carry the holder, the licence name, a
    licence URL, the terms text and an attribution line per provider —
    "we did not write the licence down" is not a loadable state.
``firewall.excluded_from``
    Required BY EQUALITY to the eight surfaces §2 fences a collection from
    (:data:`FENCED_SURFACES`). Not "at least" — exactly: a manifest that
    quietly drops ``surge_detection`` from the list is refused, because the
    firewall is a declaration the registry checks and not a default it fills
    in.
``origin_class``
    One of the three HISTORY classes only, and consistent with
    ``origin_shape`` (:data:`ORIGIN_SHAPE_CLASS`). A collection can never
    declare ``live``/``web_retrieval``/``seed`` — those are what the firewall
    exists to keep it out of.

THE MANIFEST IS DATA. ``manifest.series[*]`` is the bounded holding, and a
collection VERSION is the hash of it (:func:`manifest_hash`). The fetch shape
is declared per series in ``fetch.mode`` — ``json_api`` (one GET per series
and subject) or ``bulk_jsonl`` (one download of a provider archive, scanned
once for every pair that names it) — the two shapes
``scripts/verify_collection_manifest.py`` already reads and
``scripts/load_collection.py`` fetches through.

Lifecycle is its own four-state machine (:class:`CollectionState`) —
``draft`` → ``reviewed`` → ``loaded`` → ``superseded`` — not the
``LifecycleState`` the other four families share: a holding is never "paused"
and never "active", it is reviewed by the operator, loaded once, and one day
superseded by a newer version of the same manifest.

Mirrors the conventions in :mod:`legba.data.schemas.source` (strict,
``extra="forbid"``, content-hashed identity).
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..provenance.access import ACCESS_CLASSES
from ..provenance.origin import LIVE_CLASSES, ORIGIN_CLASSES
from .versioning import canonical_json_bytes

#: Deploy marker — the rolling deploy greps for this exact string.
COLLECTION_SCHEMA_VERSION = "2026-09/7g-1"

#: The schema URI this module implements.
COLLECTION_SCHEMA_URI = "legba/collection/1.0.0"

#: File convention, pinned by ``tests/data_pkg/test_collection_taxonomy.py``
#: the way ``source_`` is pinned by the source-class taxonomy test.
COLLECTION_FILE_PREFIX = "collection_"

CollectionId = Annotated[
    str, Field(pattern=r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*$", max_length=128)
]
_DATE_RE = r"^\d{4}-\d{2}-\d{2}$"
DateString = Annotated[str, Field(pattern=_DATE_RE)]

#: §2 — the eight surfaces a collection is fenced from, in the design note's
#: order. ``firewall.excluded_from`` must name every one of them; the set is
#: what the equality check compares against.
FENCED_SURFACES: tuple[str, ...] = (
    "cadence_analysts",
    "freshness",
    "source_health",
    "calibration",
    "salience",
    "alerts",
    "reactive_triggers",
    "surge_detection",
)

#: §2 — the readers that may opt in to history. Nothing reads history by
#: default; an opt-in reader takes an explicit ``origin_classes=`` argument.
OPT_IN_READERS: tuple[str, ...] = (
    "consult",
    "research",
    "claim_watch",
    "program6_baselines",
    "replay",
)

#: §2 — WHERE the holding comes from, and the ``origin_class`` each shape
#: stamps on every row it writes. The map is the consistency rule: a
#: manifest declaring ``archive_only`` cannot also declare
#: ``backfill_native``.
ORIGIN_SHAPE_CLASS: dict[str, str] = {
    "source_with_history": "backfill_native",
    "source_without_history": "backfill_reconstructed",
    "archive_only": "archive",
}

#: The three HISTORY classes — the only ones a collection may declare.
HISTORY_CLASSES: frozenset[str] = frozenset(ORIGIN_SHAPE_CLASS.values())

OriginShape = Literal["source_with_history", "source_without_history", "archive_only"]

#: §2 — the loader kinds. The two ``series_*`` kinds are built (7g-1); the
#: two ``documents_*`` kinds are a DECLARED SEAM (SEAMS #62) — the schema
#: accepts them so a manifest can be written and reviewed, and
#: ``scripts/load_collection.py`` refuses them loudly at the top of the load
#: rather than writing a partial holding.
LoaderKind = Literal[
    "series_api",
    "series_csv",
    "documents_wacz",
    "documents_cc_news",
]

#: The loader kinds 7g-1 actually loads.
SERIES_LOADER_KINDS: frozenset[str] = frozenset({"series_api", "series_csv"})

#: The loader kinds that are a declared seam.
DOCUMENT_LOADER_KINDS: frozenset[str] = frozenset(
    {"documents_wacz", "documents_cc_news"}
)

#: The two fetch shapes ``scripts/verify_collection_manifest.py`` reads and
#: ``scripts/load_collection.py`` fetches through.
FetchMode = Literal["json_api", "bulk_jsonl"]

SubjectKind = Literal["country", "entity", "target", "global"]


# ---------------------------------------------------------------------------
# Lifecycle — a collection's own four states
# ---------------------------------------------------------------------------


class CollectionState(str, Enum):
    """``draft`` → ``reviewed`` → ``loaded`` → ``superseded`` (§2).

    Deliberately NOT :class:`legba.data.schemas.lifecycle.LifecycleState`: a
    holding is never "paused" and never "active". ``reviewed`` is the
    operator's approval of the licence line and the manifest; ``loaded`` is
    stamped after ``scripts/load_collection.py`` has written its rows;
    ``superseded`` is terminal and means a newer version of the same manifest
    is the one to read.
    """

    DRAFT = "draft"
    REVIEWED = "reviewed"
    LOADED = "loaded"
    SUPERSEDED = "superseded"


#: The legal transitions. ``superseded`` is terminal, mirroring ``retired``
#: on the shared state machine.
COLLECTION_TRANSITIONS: dict[CollectionState, set[CollectionState]] = {
    CollectionState.DRAFT: {CollectionState.REVIEWED, CollectionState.SUPERSEDED},
    CollectionState.REVIEWED: {
        CollectionState.LOADED,
        CollectionState.DRAFT,
        CollectionState.SUPERSEDED,
    },
    CollectionState.LOADED: {CollectionState.SUPERSEDED},
    CollectionState.SUPERSEDED: set(),
}


class CollectionAbstractionLevel(str, Enum):
    """``L0`` — raw provider observations, no analysis.

    Its own enum rather than :class:`legba.data.schemas.lifecycle.AbstractionLevel`
    (L1/L2/L3), because a collection sits BELOW the raw-descriptor level those
    three describe: it is the holding an L1 descriptor would read, not a
    descriptor of analysis. The pre-approved pilot manifest declares ``L0``
    and the manifest is what this schema has to accept.
    """

    L0 = "L0"


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


class CollectionIdentity(BaseModel):
    """The content-addressed identity block.

    Carries its own ``mode='before'`` enum coercers for the same reason
    :class:`legba.data.schemas.lifecycle.WireEnumCoercion` exists — ``strict=True``
    switches pydantic's ``str`` → ``Enum`` coercion off, and the WIRE form of
    ``state`` / ``abstraction_level`` is always the bare string. The mixin
    itself is not reusable here: its coercers call ``LifecycleState(v)`` and
    ``AbstractionLevel(v)``, neither of which knows ``reviewed``/``loaded`` or
    ``L0``.
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    id: CollectionId
    name: str
    schema_uri: str = Field(pattern=r"^legba/collection/\d+\.\d+\.\d+$")
    version: str = Field(pattern=r"^[a-f0-9]{16,64}$")  # content hash
    abstraction_level: CollectionAbstractionLevel = CollectionAbstractionLevel.L0
    inherits: list[CollectionId] = Field(default_factory=list, max_length=8)
    state: CollectionState = CollectionState.DRAFT
    owner: str
    created: datetime
    retire_after: datetime | None = None

    @field_validator("state", mode="before")
    @classmethod
    def _coerce_state(cls, v: Any) -> Any:
        if isinstance(v, str) and not isinstance(v, CollectionState):
            return CollectionState(v)
        return v

    @field_validator("abstraction_level", mode="before")
    @classmethod
    def _coerce_abstraction_level(cls, v: Any) -> Any:
        if isinstance(v, str) and not isinstance(v, CollectionAbstractionLevel):
            return CollectionAbstractionLevel(v)
        return v


# ---------------------------------------------------------------------------
# Licence — the fail-closed axis
# ---------------------------------------------------------------------------


class ProviderLicence(BaseModel):
    """One provider's licence line, as the operator approved it (§7).

    Every field here is REQUIRED. A provider block that records a name but no
    URL, or a URL but no terms text, is not a recorded licence — it is a
    guess, and a guess must not be loadable. ``extra='allow'`` keeps a
    provider-specific extra URL (the World Bank's ``summary_terms_url``) from
    being a schema change.
    """

    model_config = ConfigDict(strict=True, extra="allow")

    holder: str = Field(min_length=1)
    licence: str = Field(min_length=1)
    licence_url: str = Field(min_length=1)
    text: str = Field(min_length=1)
    attribution: str = Field(min_length=1)
    terms_url: str | None = None


class LicenceBlock(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    providers: dict[str, ProviderLicence] = Field(min_length=1)


# ---------------------------------------------------------------------------
# Firewall — declared, never assumed
# ---------------------------------------------------------------------------


class FirewallBlock(BaseModel):
    """§2 — the surfaces a collection is fenced from, declared by the manifest.

    ``excluded_from`` is checked BY EQUALITY against :data:`FENCED_SURFACES`.
    A manifest that omits one of the eight is refused here, at validation,
    which is the only place the refusal is cheap: once rows are written, an
    un-fenced surface reads ten years of history as the present.
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    readers_opt_in: list[str] = Field(default_factory=list)
    excluded_from: list[str]

    @field_validator("readers_opt_in")
    @classmethod
    def _known_readers(cls, v: list[str]) -> list[str]:
        unknown = sorted(set(v) - set(OPT_IN_READERS))
        if unknown:
            raise ValueError(
                f"firewall.readers_opt_in names unknown reader(s) {unknown}; "
                f"the opt-in vocabulary is {list(OPT_IN_READERS)}"
            )
        return v

    @field_validator("excluded_from")
    @classmethod
    def _exactly_the_eight(cls, v: list[str]) -> list[str]:
        asked = set(v)
        if len(asked) != len(v):
            dupes = sorted({s for s in v if v.count(s) > 1})
            raise ValueError(f"firewall.excluded_from repeats {dupes}")
        missing = sorted(set(FENCED_SURFACES) - asked)
        unknown = sorted(asked - set(FENCED_SURFACES))
        if missing or unknown:
            raise ValueError(
                "firewall.excluded_from must name EXACTLY the eight fenced "
                f"surfaces {list(FENCED_SURFACES)}"
                + (f"; missing {missing}" if missing else "")
                + (f"; unknown {unknown}" if unknown else "")
            )
        return v


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


class LoaderBlock(BaseModel):
    """How ``scripts/load_collection.py`` walks the manifest.

    ``budget_tokens_per_day`` is declared and must be ``0``: a collection
    makes no LLM call at all — it fetches numbers and writes rows. The zero
    is the literal truth here, not a cap (house rule,
    ``docs/V3_IMPLEMENTATION_PLAN.md`` §0).
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    kind: LoaderKind
    priority: Literal["low"] = "low"
    batch: int = Field(default=200, ge=1, le=10_000)
    resume_key: str = Field(default="series_id:subject", min_length=1)
    rate_limit_per_second: dict[str, float] = Field(default_factory=dict)
    budget_tokens_per_day: int = 0

    @field_validator("budget_tokens_per_day")
    @classmethod
    def _no_llm_budget(cls, v: int) -> int:
        if v != 0:
            raise ValueError(
                "loader.budget_tokens_per_day must be 0 — a collection makes "
                "no LLM call; it fetches numbers and writes rows"
            )
        return v

    @field_validator("rate_limit_per_second")
    @classmethod
    def _positive_rates(cls, v: dict[str, float]) -> dict[str, float]:
        bad = sorted(k for k, rate in v.items() if float(rate) <= 0)
        if bad:
            raise ValueError(
                f"loader.rate_limit_per_second must be > 0 per provider; "
                f"{bad} declare a non-positive rate"
            )
        return v


# ---------------------------------------------------------------------------
# Subjects, window, provider access
# ---------------------------------------------------------------------------


class SubjectEntry(BaseModel):
    """One subject of the holding — the desk it serves and its codes.

    ``subject`` is what lands in ``observations.subject``; for a country
    collection it is the ISO-3166-1 alpha-2 code, because that is what the
    desks' ``scope.geo`` already carries. ``iso3`` is recorded because some
    provider series ids are built from alpha-3.
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    subject: str = Field(min_length=1, max_length=128)
    desk: str | None = None
    iso3: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    name: str | None = None


class WindowBlock(BaseModel):
    """The valid-time window of the WHOLE holding. No series widens past it."""

    model_config = ConfigDict(strict=True, extra="forbid")

    valid_from: DateString
    valid_to: DateString

    @model_validator(mode="after")
    def _ordered(self) -> "WindowBlock":
        if self.valid_from > self.valid_to:
            raise ValueError(
                f"window.valid_from {self.valid_from} is after "
                f"window.valid_to {self.valid_to}"
            )
        return self


class ProviderAccess(BaseModel):
    """What was MEASURED about reaching a provider, and when.

    ``requires_key`` is required: "does this provider need a credential" is
    the question that decides whether a manifest is loadable at all, and an
    unanswered one must not read as "no".
    """

    model_config = ConfigDict(strict=True, extra="allow")

    requires_key: bool
    verified: str | None = None
    note: str | None = None


class OpenItem(BaseModel):
    """Something the operator (or another lane) must settle before a load."""

    model_config = ConfigDict(strict=True, extra="forbid")

    id: str = Field(min_length=1)
    owner: str = Field(min_length=1)
    text: str = Field(min_length=1)


# ---------------------------------------------------------------------------
# The manifest
# ---------------------------------------------------------------------------


class SubjectCoverage(BaseModel):
    """What a provider was MEASURED to hold for one series and one subject.

    ``first_valid_year``/``last_valid_year`` are ``None`` when the provider
    holds nothing — absence rendered as absence, never a zero a reader could
    average. ``held: false`` plus a ``reason`` is how a real hole (the World
    Bank publishes no external-debt figure for the US in any year) is
    recorded rather than filled.
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    values: int = Field(ge=0)
    nulls: int = Field(ge=0)
    first_valid_year: int | None = None
    last_valid_year: int | None = None
    held: bool | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def _absence_is_absence(self) -> "SubjectCoverage":
        if self.values == 0 and (
            self.first_valid_year is not None or self.last_valid_year is not None
        ):
            raise ValueError(
                "coverage declares 0 values but names a first/last valid year"
            )
        if self.values > 0 and (
            self.first_valid_year is None or self.last_valid_year is None
        ):
            raise ValueError(
                "coverage declares values but no first/last valid year"
            )
        if self.held is False and self.values > 0:
            raise ValueError("coverage says held: false but declares values")
        return self


class CoverageBlock(BaseModel):
    """A series' ``coverage:`` block — the run metadata plus one block per
    subject, keyed by the subject code.

    The per-subject blocks arrive as EXTRAS (they are named ``US``, ``IR``,
    … — data, not a fixed field set), so they are validated here rather than
    declared: every extra key must parse as a :class:`SubjectCoverage`, and
    :meth:`subject_blocks` is how a reader gets them back typed. A malformed
    block raises at validation rather than being silently carried as an
    opaque dict.
    """

    model_config = ConfigDict(strict=True, extra="allow")

    fetched_at: str = Field(min_length=1)
    provider_last_updated: str | None = None
    provider_frozen: bool = False

    @model_validator(mode="after")
    def _subject_blocks_parse(self) -> "CoverageBlock":
        for key, raw in (self.__pydantic_extra__ or {}).items():
            if not isinstance(raw, dict):
                raise ValueError(
                    f"coverage.{key} is not a per-subject coverage block"
                )
            SubjectCoverage.model_validate(raw, strict=False)
        return self

    def subject_blocks(self) -> dict[str, SubjectCoverage]:
        """The per-subject coverage, typed and keyed by subject code."""
        return {
            key: SubjectCoverage.model_validate(raw, strict=False)
            for key, raw in (self.__pydantic_extra__ or {}).items()
        }


class FetchBlock(BaseModel):
    """HOW one series is fetched — the shape, not the transport.

    ``mode`` is what ``scripts/verify_collection_manifest.py`` already
    branches on and what ``scripts/load_collection.py`` fetches through, so
    the loader never guesses a provider's shape from its name.
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    mode: FetchMode = "json_api"
    requires_key: bool = False
    key_env: str | None = None
    record_time_source: str | None = None
    bulk_url: str | None = None
    bulk_member: str | None = None
    record_key: str | None = None

    @model_validator(mode="after")
    def _bulk_needs_its_archive(self) -> "FetchBlock":
        if self.mode == "bulk_jsonl" and not (self.bulk_url and self.bulk_member):
            raise ValueError(
                "fetch.mode bulk_jsonl requires fetch.bulk_url and "
                "fetch.bulk_member — the archive a series is scanned out of"
            )
        if self.requires_key and not self.key_env:
            raise ValueError(
                "fetch.requires_key is true but fetch.key_env does not name "
                "the environment variable the credential is read from"
            )
        return self


class SeriesEntry(BaseModel):
    """One ``manifest.series[*]`` entry — a (series, subjects, window) holding."""

    model_config = ConfigDict(strict=True, extra="forbid")

    series_id: str = Field(min_length=1, max_length=128)
    provider: str = Field(min_length=1, max_length=64)
    dataset: str = Field(min_length=1, max_length=64)
    indicator: str = Field(min_length=1)
    indicator_name: str | None = None
    unit: str = Field(min_length=1, max_length=64)
    cadence: str = Field(min_length=1, max_length=32)
    subject_kind: SubjectKind | None = None
    subjects: list[str] = Field(min_length=1)
    valid_from: DateString
    valid_to: DateString
    source_url_template: str = Field(min_length=1)
    fetch: FetchBlock = Field(default_factory=FetchBlock)
    coverage: CoverageBlock | None = None

    @model_validator(mode="after")
    def _ordered(self) -> "SeriesEntry":
        if self.valid_from > self.valid_to:
            raise ValueError(
                f"series {self.series_id!r}: valid_from {self.valid_from} is "
                f"after valid_to {self.valid_to}"
            )
        return self


class ManifestBlock(BaseModel):
    """The bounded holding. A collection with no series is not a collection."""

    model_config = ConfigDict(strict=True, extra="forbid")

    series: list[SeriesEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _not_empty(self) -> "ManifestBlock":
        if not self.series:
            raise ValueError(
                "manifest.series is empty — a collection with no series is "
                "not a collection, and must not register as one"
            )
        return self


# ---------------------------------------------------------------------------
# The descriptor
# ---------------------------------------------------------------------------


class CollectionDescriptor(BaseModel):
    """``legba/collection/1.0.0`` — one bounded, versioned holding."""

    model_config = ConfigDict(strict=True, extra="forbid")

    identity: CollectionIdentity
    origin_shape: OriginShape
    origin_class: str
    licence_class: str
    licence: LicenceBlock
    firewall: FirewallBlock
    loader: LoaderBlock
    manifest: ManifestBlock
    subject_kind: SubjectKind = "country"
    subjects: list[SubjectEntry] = Field(min_length=1)
    window: WindowBlock
    provider_access: dict[str, ProviderAccess] = Field(default_factory=dict)
    open_items: list[OpenItem] = Field(default_factory=list)

    @field_validator("licence_class")
    @classmethod
    def _licence_class_in_vocabulary(cls, v: str) -> str:
        if v not in ACCESS_CLASSES:
            raise ValueError(
                f"licence_class {v!r} is not in the access-class vocabulary "
                f"{list(ACCESS_CLASSES)} — a collection whose class is not "
                "recorded does not load (design note §D.6, fail-closed)"
            )
        return v

    @field_validator("origin_class")
    @classmethod
    def _origin_class_is_history(cls, v: str) -> str:
        if v not in ORIGIN_CLASSES:
            raise ValueError(
                f"origin_class {v!r} is not in the closed vocabulary "
                f"{list(ORIGIN_CLASSES)}"
            )
        if v in LIVE_CLASSES:
            raise ValueError(
                f"origin_class {v!r} is a LIVE class — a collection is "
                f"history and must declare one of {sorted(HISTORY_CLASSES)}"
            )
        return v

    @model_validator(mode="after")
    def _constraints(self) -> "CollectionDescriptor":
        expected = ORIGIN_SHAPE_CLASS[self.origin_shape]
        if self.origin_class != expected:
            raise ValueError(
                f"origin_shape {self.origin_shape!r} stamps "
                f"origin_class {expected!r}, but the manifest declares "
                f"{self.origin_class!r}"
            )
        declared = {s.subject for s in self.subjects}
        if len(declared) != len(self.subjects):
            raise ValueError("subjects repeats a subject code")
        for series in self.manifest.series:
            unknown = sorted(set(series.subjects) - declared)
            if unknown:
                raise ValueError(
                    f"series {series.series_id!r} names subject(s) {unknown} "
                    f"that the top-level `subjects:` block does not declare "
                    f"(declared: {sorted(declared)})"
                )
            if series.valid_from < self.window.valid_from or (
                series.valid_to > self.window.valid_to
            ):
                raise ValueError(
                    f"series {series.series_id!r} window "
                    f"{series.valid_from}..{series.valid_to} widens past the "
                    f"collection window {self.window.valid_from}.."
                    f"{self.window.valid_to}"
                )
            if series.fetch.mode == "bulk_jsonl" and self.loader.kind not in (
                SERIES_LOADER_KINDS
            ):
                raise ValueError(
                    f"series {series.series_id!r} declares fetch.mode "
                    "bulk_jsonl under a non-series loader kind "
                    f"{self.loader.kind!r}"
                )
            needs_iso3 = "{iso3}" in series.indicator or (
                "{iso3}" in series.source_url_template
            )
            if needs_iso3:
                missing = sorted(
                    s.subject
                    for s in self.subjects
                    if s.subject in set(series.subjects) and not s.iso3
                )
                if missing:
                    raise ValueError(
                        f"series {series.series_id!r} substitutes {{iso3}} but "
                        f"subject(s) {missing} carry no `iso3`"
                    )
        provider_names = {s.provider for s in self.manifest.series}
        missing_licence = sorted(provider_names - set(self.licence.providers))
        if missing_licence:
            raise ValueError(
                f"provider(s) {missing_licence} publish series in this "
                "manifest but carry no `licence.providers` block — a "
                "collection without a recorded licence line does not load"
            )
        return self

    def manifest_hash(self) -> str:
        """The collection VERSION: the hash of the manifest (§2).

        A collection version is the hash of its manifest and NOT of the whole
        descriptor, so re-approving a licence line or adding an open item
        does not invalidate a load that already happened. Rendered as the
        first 16 hex characters of the SHA-256 over the canonical JSON of
        ``manifest``, which is the form ``collection_loads.collection_version``
        carries.
        """
        return manifest_hash(self.manifest)

    def series_by_id(self) -> dict[str, SeriesEntry]:
        return {s.series_id: s for s in self.manifest.series}

    def iso3_for(self, subject: str) -> str | None:
        for entry in self.subjects:
            if entry.subject == subject:
                return entry.iso3
        return None


def manifest_hash(manifest: ManifestBlock) -> str:
    """The 16-hex-character content hash of a manifest block."""
    payload = manifest.model_dump(mode="json", exclude_none=True)
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()[:16]


__all__ = [
    "COLLECTION_FILE_PREFIX",
    "COLLECTION_SCHEMA_URI",
    "COLLECTION_SCHEMA_VERSION",
    "COLLECTION_TRANSITIONS",
    "CollectionAbstractionLevel",
    "CollectionDescriptor",
    "CollectionIdentity",
    "CollectionState",
    "CoverageBlock",
    "DOCUMENT_LOADER_KINDS",
    "FENCED_SURFACES",
    "FetchBlock",
    "FirewallBlock",
    "HISTORY_CLASSES",
    "LicenceBlock",
    "LoaderBlock",
    "ManifestBlock",
    "OPT_IN_READERS",
    "ORIGIN_SHAPE_CLASS",
    "OpenItem",
    "ProviderAccess",
    "ProviderLicence",
    "SERIES_LOADER_KINDS",
    "SeriesEntry",
    "SubjectCoverage",
    "SubjectEntry",
    "WindowBlock",
    "manifest_hash",
]
