# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The per-profile PROJECTION — the leaf both sides read (W9 VIEW-1).

A profile's case-shaped vocabulary — its subject groups, its unit roster, its
tier labels, the Morning Read's bands, the absence route's scope resolution,
its crosswalk rows — used to be code constants a reader carried. This module
is the leaf of the replacement, exactly ``graph_arcs``' four parts
(Review A §2.2d):

  * the view — ``profiles/<id>/projection/view.yaml``, declared by the
    manifest's ``projection: {view, plane}`` key and loaded here through a
    CLOSED model (:class:`ProjectionView`);
  * the builder — ``deterministic_handlers/profile_projector.py``, a
    whole rebuild per plane into the ``profile_projection`` table;
  * the table — migration ``0229_profile_projection.sql``: one row per
    ``(plane, section, key)`` plus a ``_meta`` row carrying ``projected_at``;
  * the route — ``registry/projection_api.py``, refusing by name.

PLANES. Each profile's projection is a separate PLANE (the ``plane`` column):
two profiles on one substrate never read each other's rows, and a rebuild of
one plane never touches another's. A profile that declares no projection still
HAS a plane id (its own id) — every reader of it refuses
``projection_disabled``, by name.

WHERE THE VIEWS COME FROM. The images carry ``src/`` only (no ``profiles/``),
so every profile's view, RESOLVED against its manifest (its units, tiers and
labels), ships inside the package as ``profile_projection.json``
(``scripts/gen_profile_projection.py``; the test suite fails when it is
stale) — the same way ``profile_rosters.json`` ships the rosters.
``LEGBA_PROFILE`` set ⇒ that profile directory is resolved from disk instead
(and a profile that does not load is an error, never a silent fallback).

THE CONTRACT (the ``graph_projection`` vocabulary, one set of words):

  * ``LEGBA_PROFILE_PROJECTION`` (default OFF) — off, the builder no-ops and
    every reader answers ``projection_disabled``;
  * ``profile_projection._meta.projected_at`` is published on every read and a
    reader past ``LEGBA_PROFILE_PROJECTION_MAX_AGE_SECONDS`` (default 7200 s)
    refuses ``projection_stale``; a plane never built (or the table not yet
    migrated) answers ``projection_empty`` — never ``found=False``.

THE CROSSWALK (DEC-12). A platform vocabulary family ``crosswalk`` whose entry
``value`` is ``<profile>.<axis>:<value>`` and whose ``parent`` is the platform
value it maps to (``<platform_family>:<value>``). :func:`resolve_crosswalk`
answers one lookup; an unmapped value returns ``None`` and is left alone.

A LEAF: stdlib, pydantic and yaml, plus the ``profile`` / ``rosters`` /
``graph_projection`` leaves and ``schemas.vocabulary`` — importable from the
slim registry image and the runtime alike.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Iterable, Literal, Mapping

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .graph_projection import (
    PROJECTION_DISABLED,
    PROJECTION_EMPTY,
    PROJECTION_OK,
    PROJECTION_STALE,
)
from .profile import (
    MANIFEST_NAME,
    PROFILE_ENV,
    Profile,
    ProfileError,
    load_profile,
    resolve_profile,
)
from .schemas.vocabulary import CROSSWALK_FAMILY, PLATFORM_FAMILIES, validate_family

logger = logging.getLogger(__name__)

__all__ = [
    "CROSSWALK_FAMILY",
    "DEFAULT_MAX_AGE_SECONDS",
    "EXPORT_SCHEMA",
    "META_KEY",
    "META_SECTION",
    "PACKAGED_EXPORT",
    "PLANE_UNKNOWN",
    "PROFILE_PROJECTION_ENV",
    "PROFILE_PROJECTION_MAX_AGE_ENV",
    "PROJECTION_DISABLED",
    "PROJECTION_EMPTY",
    "PROJECTION_OK",
    "PROJECTION_STALE",
    "SECTIONS",
    "SECTION_UNDECLARED",
    "VIEW_SCHEMA",
    "CrosswalkPair",
    "PlaneCatalog",
    "ProjectionView",
    "ScopeTable",
    "catalog_of",
    "crosswalk_pair",
    "export_planes",
    "load_view",
    "plane_catalog",
    "profile_projection_enabled",
    "projection_max_age_seconds",
    "projection_state",
    "read_plane",
    "read_section",
    "refresh",
    "resolve_crosswalk",
    "resolve_plane",
    "scope_resolution",
]

#: The kill switch. Default OFF.
PROFILE_PROJECTION_ENV = "LEGBA_PROFILE_PROJECTION"
#: How stale a plane's build may be before readers refuse ``projection_stale``.
PROFILE_PROJECTION_MAX_AGE_ENV = "LEGBA_PROFILE_PROJECTION_MAX_AGE_SECONDS"
#: The projector descriptor runs hourly; the default is one missed tick of
#: grace — the ``graph_projection`` default.
DEFAULT_MAX_AGE_SECONDS = 7_200.0

#: Two refusals beyond the shared three: a plane no profile declares, and a
#: section the plane's view does not carry. Both are 404s with a named state.
PLANE_UNKNOWN = "plane_unknown"
SECTION_UNDECLARED = "section_undeclared"

#: The closed section vocabulary, in a plane's reading order.
SECTIONS: tuple[str, ...] = (
    "subject_groups", "units", "tiers", "labels", "bands", "scope", "crosswalk",
)
#: The build receipt row: ``(plane, '_meta', 'build')``.
META_SECTION = "_meta"
META_KEY = "build"

#: The ``schema`` stamp of a view file and of the packaged export.
VIEW_SCHEMA = "legba/projection_view/1"
EXPORT_SCHEMA = "legba/profile_projection/1"
#: The packaged copy of every profile's resolved view (see the module doc).
PACKAGED_EXPORT = Path(__file__).with_name("profile_projection.json")

_Slug = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)]
_Text = Annotated[str, Field(min_length=1, max_length=400)]


# ---------------------------------------------------------------------------
# The view file — a CLOSED model
# ---------------------------------------------------------------------------


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class SubjectGroupsSpec(_Closed):
    """The ``frame`` family's subtrees this plane carries, by kind."""

    kinds: tuple[_Slug, ...] = Field(min_length=1)


class UnitsSpec(_Closed):
    """The profile's unit roster; ``cadence_from: descriptor_head`` reads each
    unit's own cadence off its live head at build time."""

    cadence_from: Literal["descriptor_head", "none"] = "descriptor_head"


class BandSpec(_Closed):
    id: _Slug
    label: _Text
    question: _Text


class ScopeSpec(_Closed):
    """The absence route's scope-resolution table (the unit list is the
    profile's roster, not restated here)."""

    banding_analyst: _Slug
    audit_analyst: _Slug
    unit_grace_multiple: float = Field(gt=0)


class CrosswalkSpec(_Closed):
    #: The profile namespace whose crosswalk rows this plane carries — the
    #: profile's own id (a profile reads only its own namespace).
    namespace: _Slug


class ProjectionView(_Closed):
    """``profiles/<id>/projection/view.yaml``. Each key is one section; a key
    left out is a section the plane does not carry."""

    schema_: Literal["legba/projection_view/1"] = Field(alias="schema")
    subject_groups: SubjectGroupsSpec | None = None
    units: UnitsSpec | None = None
    tiers: Literal["ui.frames"] | None = None
    labels: Literal["ui.labels"] | None = None
    bands: tuple[BandSpec, ...] | None = None
    scope: ScopeSpec | None = None
    crosswalk: CrosswalkSpec | None = None

    @model_validator(mode="after")
    def _unique(self) -> "ProjectionView":
        for what, ids in (
            ("subject_groups.kinds", list(self.subject_groups.kinds) if self.subject_groups else []),
            ("bands", [b.id for b in self.bands or ()]),
        ):
            dup = sorted({i for i in ids if ids.count(i) > 1})
            if dup:
                raise ValueError(f"{what} ids repeat: {dup}")
        return self


def load_view(profile: Profile) -> ProjectionView | None:
    """The profile's view, validated; ``None`` when it declares no projection.

    Raises :class:`ProfileError` when the declared file is missing or is not a
    mapping, pydantic's ``ValidationError`` for an unknown or malformed key.
    """
    path = profile.projection_view_path
    if path is None:
        return None
    if not path.is_file():
        raise ProfileError(f"{profile.manifest_path}: projection.view names no file: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ProfileError(f"{path} is not a mapping")
    return ProjectionView.model_validate(data)


# ---------------------------------------------------------------------------
# Resolving a view against its manifest (the static half of a plane)
# ---------------------------------------------------------------------------


def _rows(pairs: Iterable[tuple[str, Any]]) -> list[dict[str, Any]]:
    return [{"key": k, "value": v} for k, v in pairs]


def _label_rows(profile: Profile) -> list[dict[str, Any]]:
    ui = profile.ui
    pairs: list[tuple[str, Any]] = [
        ("subject.singular", ui.subject.singular),
        ("subject.plural", ui.subject.plural),
    ]
    for k in ui.subject.kinds:
        pairs.append((f"subject.kinds.{k.id}", {"singular": k.singular, "plural": k.plural}))
    if ui.unit_noun is not None:
        pairs += [("unit_noun.singular", ui.unit_noun.singular),
                  ("unit_noun.plural", ui.unit_noun.plural)]
    if ui.landing is not None:
        pairs.append(("landing", ui.landing))
    if ui.map.title is not None:
        pairs.append(("map.title", ui.map.title))
    lab = ui.labels
    for name in ("read_title", "overview_title", "unscoped_subject"):
        val = getattr(lab, name)
        if val is not None:
            pairs.append((name, val))
    for tag, text in lab.roster.items():
        pairs.append((f"roster.{tag}", text))
    return _rows(pairs)


def resolve_plane(profile: Profile) -> dict[str, Any]:
    """One profile's plane entry: ``{profile, plane, view}`` where ``view`` is
    ``None`` (no projection declared) or ``{digest, sections, derive}``:
    ``sections`` holds the rows resolved from the manifest now (tiers, labels,
    bands, scope, the units' static half) and ``derive`` what the builder
    reads off the substrate (subject groups, unit cadences, crosswalk rows).
    """
    view = load_view(profile)
    entry: dict[str, Any] = {"profile": profile.id, "plane": profile.plane, "view": None}
    if view is None:
        return entry
    units = [u.id for u in profile.rosters.units]
    scored = set(profile.rosters.scored_units)
    sections: dict[str, list[dict[str, Any]]] = {}
    derive: dict[str, Any] = {}
    if view.subject_groups is not None:
        sections["subject_groups"] = []
        derive["subject_groups"] = {"kinds": list(view.subject_groups.kinds)}
    if view.units is not None:
        sections["units"] = _rows((u, {"scored": u in scored}) for u in units)
        if view.units.cadence_from == "descriptor_head":
            derive["units"] = {"cadence_from": "descriptor_head"}
    if view.tiers is not None:
        sections["tiers"] = _rows(
            (f.id, {"label": f.label, "parent": f.parent, "producer": f.producer})
            for f in profile.ui.frames
        )
    if view.labels is not None:
        sections["labels"] = _label_rows(profile)
    if view.bands is not None:
        sections["bands"] = _rows((b.id, {"label": b.label, "question": b.question}) for b in view.bands)
    if view.scope is not None:
        sections["scope"] = _rows([
            ("units", units),
            ("banding_analyst", view.scope.banding_analyst),
            ("audit_analyst", view.scope.audit_analyst),
            ("unit_grace_multiple", view.scope.unit_grace_multiple),
        ])
    if view.crosswalk is not None:
        if view.crosswalk.namespace != profile.id:
            raise ProfileError(
                f"{profile.projection_view_path}: crosswalk.namespace "
                f"{view.crosswalk.namespace!r} is not the profile's own ({profile.id!r})"
            )
        sections["crosswalk"] = []
        derive["crosswalk"] = {"namespace": view.crosswalk.namespace}
    body = {"sections": sections, "derive": derive}
    digest = hashlib.sha256(
        json.dumps(body, sort_keys=True, ensure_ascii=True).encode("utf-8")
    ).hexdigest()
    entry["view"] = {"digest": digest, **body}
    return entry


def export_planes(profiles: Iterable[Profile], *, default_plane: str | None) -> str:
    """The text of :data:`PACKAGED_EXPORT`: every profile's plane entry, keys
    sorted, two-space indent, trailing newline — deterministic bytes. Two
    profiles declaring one plane is refused."""
    planes: dict[str, Any] = {}
    for p in profiles:
        entry = resolve_plane(p)
        if entry["plane"] in planes:
            raise ProfileError(
                f"plane {entry['plane']!r} is declared by both "
                f"{planes[entry['plane']]['profile']!r} and {p.id!r}"
            )
        planes[entry["plane"]] = entry
    if default_plane is not None and default_plane not in planes:
        raise ProfileError(f"default plane {default_plane!r} is not one of {sorted(planes)}")
    doc = {
        "_generated": "scripts/gen_profile_projection.py from profiles/*/" + MANIFEST_NAME
        + " and their projection views - do not edit by hand",
        "schema": EXPORT_SCHEMA,
        "default_plane": default_plane,
        "planes": planes,
    }
    return json.dumps(doc, sort_keys=True, indent=2, ensure_ascii=True) + "\n"


# ---------------------------------------------------------------------------
# The plane catalog (what this process knows)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlaneCatalog:
    """Every plane this process knows, and the one it reads by default (the
    selected profile's)."""

    planes: Mapping[str, Mapping[str, Any]]
    default_plane: str | None
    #: ``profile:<manifest>`` or ``packaged:<file>``.
    origin: str

    def entry(self, plane: str) -> Mapping[str, Any] | None:
        return self.planes.get(plane)

    def view(self, plane: str) -> Mapping[str, Any] | None:
        entry = self.planes.get(plane)
        return None if entry is None else entry.get("view")

    def declared_sections(self, plane: str) -> tuple[str, ...]:
        view = self.view(plane) or {}
        have = set((view.get("sections") or {}))
        return tuple(s for s in SECTIONS if s in have)


def _catalog_from_doc(doc: Any, origin: str) -> PlaneCatalog:
    if not isinstance(doc, dict) or doc.get("schema") != EXPORT_SCHEMA:
        raise ProfileError(f"{origin} is not a {EXPORT_SCHEMA} export")
    planes = doc.get("planes")
    if not isinstance(planes, dict):
        raise ProfileError(f"{origin}: planes is not a mapping")
    return PlaneCatalog(planes=planes, default_plane=doc.get("default_plane"), origin=origin)


def _select(env: Mapping[str, str] | None = None) -> PlaneCatalog:
    env = os.environ if env is None else env
    if env.get(PROFILE_ENV):
        profile = load_profile(resolve_profile(env=env))
        text = export_planes([profile], default_plane=profile.plane)
        return _catalog_from_doc(json.loads(text), f"profile:{profile.manifest_path}")
    doc = json.loads(PACKAGED_EXPORT.read_text(encoding="utf-8"))
    return _catalog_from_doc(doc, f"packaged:{PACKAGED_EXPORT}")


_lock = threading.Lock()
_catalog: PlaneCatalog | None = None


def plane_catalog() -> PlaneCatalog:
    """The process's plane catalog (read once, then cached)."""
    global _catalog
    cat = _catalog
    if cat is None:
        with _lock:
            if _catalog is None:
                _catalog = _select()
            cat = _catalog
    return cat


def refresh(catalog: PlaneCatalog | None = None) -> PlaneCatalog:
    """Drop the cache and read again (or install ``catalog`` — a test)."""
    global _catalog
    with _lock:
        _catalog = catalog if catalog is not None else _select()
        return _catalog


def catalog_of(profiles: Iterable[Profile], *, default_plane: str | None) -> PlaneCatalog:
    """A catalog over loaded profiles (tests, bring-up scripts)."""
    return _catalog_from_doc(
        json.loads(export_planes(profiles, default_plane=default_plane)), "profiles:<in-memory>",
    )


# ---------------------------------------------------------------------------
# Flag, staleness, the shared refusal read
# ---------------------------------------------------------------------------


def profile_projection_enabled() -> bool:
    """``LEGBA_PROFILE_PROJECTION`` — only 1/true/yes/on enable it."""
    raw = os.environ.get(PROFILE_PROJECTION_ENV, "0").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def projection_max_age_seconds() -> float:
    """``LEGBA_PROFILE_PROJECTION_MAX_AGE_SECONDS`` (default 7200 s)."""
    raw = os.environ.get(PROFILE_PROJECTION_MAX_AGE_ENV, "").strip()
    if not raw:
        return DEFAULT_MAX_AGE_SECONDS
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_MAX_AGE_SECONDS


#: The build receipt of one plane. Schema-qualified (the AGE pool's
#: search_path puts ag_catalog first).
_META_SQL = """
SELECT projected_at, value
  FROM public.profile_projection
 WHERE plane = $1 AND section = '_meta' AND key = 'build'
"""

#: One section of one plane, in its declared order — the route's read.
_SECTION_SQL = """
SELECT key, ord, value
  FROM public.profile_projection
 WHERE plane = $1 AND section = $2
 ORDER BY ord, key
"""

#: Whether migration 0229 is applied — never raises.
_TABLE_EXISTS_SQL = "SELECT to_regclass('public.profile_projection') IS NOT NULL"

#: Every section of one plane (the receipt row excluded).
_PLANE_SQL = """
SELECT section, key, ord, value
  FROM public.profile_projection
 WHERE plane = $1 AND section <> '_meta'
 ORDER BY section, ord, key
"""


def _json(raw: Any) -> Any:
    if isinstance(raw, (str, bytes, bytearray)):
        try:
            return json.loads(raw)
        except ValueError:
            return None
    return raw


async def projection_state(
    conn: Any,
    plane: str,
    *,
    now: datetime | None = None,
    catalog: PlaneCatalog | None = None,
) -> tuple[str, dict[str, Any] | None]:
    """``(state, meta)`` for one plane. The ORDER is the contract:

    flag off → ``projection_disabled``; a plane no profile declares →
    ``plane_unknown``; a plane whose profile declares no view →
    ``projection_disabled``; no receipt row (never built, or the table not
    migrated) or zero rows → ``projection_empty``; a receipt older than the
    max age → ``projection_stale``; else ``ok``. ``meta`` is the receipt
    (``projected_at``, ``age_seconds``, ``max_age_seconds`` + its value) when
    one was read, and carries ``reason`` for a disabled plane.
    """
    if not profile_projection_enabled():
        return PROJECTION_DISABLED, {
            "reason": f"{PROFILE_PROJECTION_ENV} is off — no plane is built or read",
        }
    cat = catalog or plane_catalog()
    entry = cat.entry(plane)
    if entry is None:
        return PLANE_UNKNOWN, {"reason": f"no profile declares plane {plane!r}", "planes": sorted(cat.planes)}
    if entry.get("view") is None:
        return PROJECTION_DISABLED, {
            "reason": f"profile {entry.get('profile')!r} declares no projection",
        }
    try:
        row = await conn.fetchrow(_META_SQL, plane)
    except Exception:  # noqa: BLE001 — table absent reads as never built
        return PROJECTION_EMPTY, None
    if row is None:
        return PROJECTION_EMPTY, None
    meta = dict(_json(row["value"]) or {})
    projected_at = row["projected_at"]
    if projected_at is not None and projected_at.tzinfo is None:
        projected_at = projected_at.replace(tzinfo=timezone.utc)
    max_age = projection_max_age_seconds()
    meta.update(projected_at=projected_at, max_age_seconds=max_age)
    if int(meta.get("row_count") or 0) <= 0:
        return PROJECTION_EMPTY, meta
    if projected_at is not None:
        age = ((now or datetime.now(timezone.utc)) - projected_at).total_seconds()
        meta["age_seconds"] = age
        if age > max_age:
            return PROJECTION_STALE, meta
    return PROJECTION_OK, meta


async def read_section(conn: Any, plane: str, section: str) -> list[dict[str, Any]]:
    """``[{key, ord, value}]`` of one section, in order. No state check — the
    caller has asked :func:`projection_state` first."""
    return [
        {"key": r["key"], "ord": int(r["ord"]), "value": _json(r["value"])}
        for r in await conn.fetch(_SECTION_SQL, plane, section)
    ]


async def read_plane(conn: Any, plane: str) -> dict[str, list[dict[str, Any]]]:
    """Every section of one plane, ``{section: [{key, ord, value}]}``."""
    out: dict[str, list[dict[str, Any]]] = {}
    for r in await conn.fetch(_PLANE_SQL, plane):
        out.setdefault(r["section"], []).append(
            {"key": r["key"], "ord": int(r["ord"]), "value": _json(r["value"])}
        )
    return out


# ---------------------------------------------------------------------------
# The crosswalk (DEC-12)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CrosswalkPair:
    """``<from_family>:<from_value>`` → ``<to_family>:<to_value>``."""

    from_family: str
    from_value: str
    to_family: str
    to_value: str
    notes: str | None = None

    @property
    def key(self) -> str:
        return f"{self.from_family}:{self.from_value}"


def crosswalk_pair(value: str, parent: str | None, notes: str | None = None) -> CrosswalkPair | None:
    """One ``crosswalk`` row as a pair, or ``None`` when it is not a well-formed
    one (the entry model refuses those at write; a row that reached the table
    some other way is skipped here, never guessed at)."""
    fam, sep, val = str(value or "").partition(":")
    if not sep or "." not in fam or not val or val != val.strip():
        return None
    try:
        validate_family(fam)
    except ValueError:
        return None
    pfam, psep, pval = str(parent or "").partition(":")
    if not psep or pfam not in PLATFORM_FAMILIES or pfam == CROSSWALK_FAMILY:
        return None
    if not pval.strip() or ":" in pval:
        return None
    return CrosswalkPair(fam, val, pfam, pval, notes)


def resolve_crosswalk(
    pairs: Iterable[CrosswalkPair], family: str, value: str,
) -> tuple[str, str] | None:
    """The platform ``(family, value)`` a profile's ``family:value`` maps to,
    or ``None`` — UNMAPPED: the caller leaves the value alone."""
    for p in pairs:
        if p.from_family == family and p.from_value == value:
            return p.to_family, p.to_value
    return None


# ---------------------------------------------------------------------------
# The ONE reader this wave: the absence route's scope resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScopeTable:
    """What the absence route resolves a scope against: the units it checks,
    the analysts whose runs stamp the banding and audit kinds, and the grace
    over a unit's cadence. ``origin`` says where it came from (it is not part
    of the route's output)."""

    units: tuple[str, ...]
    banding_analyst: str
    audit_analyst: str
    unit_grace_multiple: float
    origin: str = "constants"


def _scope_from_rows(rows: list[dict[str, Any]], origin: str) -> ScopeTable | None:
    by_key = {r["key"]: r["value"] for r in rows}
    units = by_key.get("units")
    banding = by_key.get("banding_analyst")
    audit = by_key.get("audit_analyst")
    grace = by_key.get("unit_grace_multiple")
    if (
        not isinstance(units, list) or not all(isinstance(u, str) and u for u in units)
        or not isinstance(banding, str) or not banding
        or not isinstance(audit, str) or not audit
        or isinstance(grace, bool) or not isinstance(grace, (int, float)) or grace <= 0
    ):
        return None
    return ScopeTable(tuple(units), banding, audit, float(grace), origin)


async def scope_resolution(
    conn: Any,
    *,
    default: ScopeTable,
    plane: str | None = None,
    now: datetime | None = None,
) -> ScopeTable:
    """The scope table from the projection when ``LEGBA_PROFILE_PROJECTION`` is
    on and the plane (default: this process's profile's) is ``ok``; otherwise
    ``default`` — today's constants. Flag off issues NO query. Never raises:
    any refusal, a malformed table or a read error is ``default`` (logged), and
    the reads run inside a (sub)transaction so a failure cannot poison a
    caller's open transaction."""
    if not profile_projection_enabled():
        return default
    try:
        cat = plane_catalog()
        target = plane or cat.default_plane
        if not target:
            return default
        # A missing table (0229 not applied) is a refusal, not an error that
        # could abort a caller's open transaction.
        if not await conn.fetchval(_TABLE_EXISTS_SQL):
            return default
        async with conn.transaction():
            state, meta = await projection_state(conn, target, now=now, catalog=cat)
            if state != PROJECTION_OK:
                logger.info("profile_projection.scope_fallback plane=%s state=%s", target, state)
                return default
            rows = await read_section(conn, target, "scope")
        at = (meta or {}).get("projected_at")
        table = _scope_from_rows(rows, f"projection:{target}@{at.isoformat() if at else '?'}")
        if table is None:
            logger.warning("profile_projection.scope_malformed plane=%s", target)
            return default
        return table
    except Exception as exc:  # noqa: BLE001 — degrade to the constants, by name
        logger.warning("profile_projection.scope_unreadable err_class=%s", type(exc).__name__)
        return default
