# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""FRAMES — a composition's subject groups as registered vocabulary (W7 FRAME-1).

A FRAME is a subject group a composition reads over: the world read floors on
one block per frame, a frame's rollup walks the frame's member roster, and the
coverage ledger names every frame that produced nothing. Until this module the
only way the platform could tell a frame from a subject was an id prefix and a
tag convention ("a frame's own id is a tag on its members"), spelled in code.

THE FAMILY. ``frame`` is a platform vocabulary family. Each entry is one frame:

    value     the frame id (for a frame bound to a target head, that head's id)
    parent    ``frame:<kind>`` — the frame's kind is itself an entry, and the
              parent link is what makes a kind's roster enumerable
    aliases   THE MEMBER RULE — one selector per alias:
                ``tag:<t>``      every active target head whose ``scope.tags``
                                 carries ``<t>`` is a member
                ``analyst:<id>`` the analyst lane ``<id>`` is the member (a
                                 cross-group lane; it has no target heads)
              A kind carries no member rule of its own: its members are the
              union of its descendants' (the parent walk).
    notes     the human label (first line)

Membership is never the frame's own head: every frame id in the subtree being
resolved is excluded, which is the old ``descriptor_id <> $1`` generalised.

THE FALLBACK. When the family is not loadable — no rows yet, a row that does
not validate, or ``frame`` refused as a family — the caller's LEGACY rule runs
instead: :class:`LegacyFrames`, the id-prefix + roster-tag convention the
family replaces, with the very SQL the composition slice has always issued.
This module names no exemplar id; the caller hands it the legacy convention.

THE FLAG. ``LEGBA_FRAME_FAMILY`` (default OFF). Off, no caller reaches this
module at all and every read is byte-for-byte the pre-W7 path. On, the
composition slice resolves its frame roster and members here.

A LEAF: imports only ``schemas.vocabulary`` (pydantic). Importable from the
slim registry image and from the runtime alike.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from .schemas.vocabulary import (
    FamilyError,
    VocabularyEntry,
    split_parent,
    validate_family,
    walk_parents,
)

logger = logging.getLogger(__name__)

#: The vocabulary family (a PLATFORM family — ``PLATFORM_FAMILIES``).
FRAME_FAMILY: str = "frame"

#: The flag. Unset / anything but a truthy token ⇒ OFF.
FRAME_FAMILY_ENV: str = "LEGBA_FRAME_FAMILY"

#: Member-rule selector kinds (the ``<kind>:`` head of a frame entry's alias).
MEMBER_TAG: str = "tag"
MEMBER_ANALYST: str = "analyst"
MEMBER_SELECTORS: tuple[str, ...] = (MEMBER_TAG, MEMBER_ANALYST)

#: :attr:`FrameCatalog.status` tokens.
STATUS_LOADED: str = "loaded"
STATUS_EMPTY: str = "empty"
STATUS_UNLOADED: str = "unloaded"


def frame_family_enabled() -> bool:
    """Is ``LEGBA_FRAME_FAMILY`` on? The house truthy parse; default OFF."""
    raw = os.getenv(FRAME_FAMILY_ENV)
    if raw is None:
        return False
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Frame:
    """One frame: its id, human label, parent link and member rule."""

    id: str
    label: str
    parent: str | None
    members: tuple[str, ...]

    def _selected(self, kind: str) -> tuple[str, ...]:
        out: list[str] = []
        for sel in self.members:
            head, sep, val = sel.partition(":")
            if sep and head == kind and val:
                out.append(val)
        return tuple(out)

    @property
    def member_tags(self) -> tuple[str, ...]:
        return self._selected(MEMBER_TAG)

    @property
    def member_analysts(self) -> tuple[str, ...]:
        return self._selected(MEMBER_ANALYST)

    @property
    def parent_id(self) -> str | None:
        """The parent frame's id when the parent is in this family."""
        if not self.parent:
            return None
        fam, val = split_parent(self.parent, default_family=FRAME_FAMILY)
        return val if fam == FRAME_FAMILY else None


@dataclass(frozen=True)
class LegacyFrames:
    """The convention the family replaces — the FALLBACK when it is unloaded.

    ``id_prefix``: a frame id starts with it. ``kind``: the tag every frame
    head carries (the roster key). Members: the frame's own id as a tag on
    its members, the frame itself excluded.
    """

    id_prefix: str
    kind: str

    def frame(self, frame_id: Any) -> Frame | None:
        fid = str(frame_id or "")
        if not fid or not fid.startswith(self.id_prefix):
            return None
        return Frame(
            id=fid, label=fid, parent=f"{FRAME_FAMILY}:{self.kind}",
            members=(f"{MEMBER_TAG}:{fid}",),
        )


def _label(entry: VocabularyEntry) -> str:
    first = (entry.notes or "").strip().splitlines()
    return first[0].strip() if first and first[0].strip() else entry.value


class FrameCatalog:
    """The ``frame`` family, as loaded — or the reason it is not."""

    def __init__(
        self,
        entries: Iterable[VocabularyEntry] = (),
        *,
        status: str = STATUS_LOADED,
        detail: str | None = None,
    ):
        self.status = status
        self.detail = detail
        self._index: dict[tuple[str, str], VocabularyEntry] = {}
        self._frames: dict[str, Frame] = {}
        for e in entries:
            if e.family != FRAME_FAMILY or e.deprecated is not None:
                continue
            self._index[(e.family, e.value)] = e
            self._frames[e.value] = Frame(
                id=e.value, label=_label(e), parent=e.parent,
                members=tuple(e.aliases),
            )

    @classmethod
    def unloaded(cls, detail: str) -> "FrameCatalog":
        return cls((), status=STATUS_UNLOADED, detail=detail)

    @property
    def loaded(self) -> bool:
        return self.status == STATUS_LOADED and bool(self._frames)

    def composite_frame(self, frame_id: Any) -> Frame | None:
        return self._frames.get(str(frame_id or ""))

    def ancestors(self, frame_id: str) -> list[str]:
        """Frame ids on the parent chain, nearest first (bounded, cycle-safe)."""
        return [
            val for fam, val in walk_parents(self._index, FRAME_FAMILY, str(frame_id))
            if fam == FRAME_FAMILY
        ]

    def descendants(self, frame_id: str) -> list[Frame]:
        """Every frame whose parent walk reaches ``frame_id``, by id."""
        fid = str(frame_id)
        return [
            f for k, f in sorted(self._frames.items())
            if k != fid and fid in self.ancestors(k)
        ]

    def subtree(self, frame_id: str) -> list[Frame]:
        own = self.composite_frame(frame_id)
        return ([own] if own else []) + self.descendants(frame_id)

    def groups(self, kind: str) -> list[Frame]:
        """The subject groups under ``kind``: its descendants that carry a
        member rule (a kind-of-kinds contributes none of its own), by id."""
        return [f for f in self.descendants(kind) if f.members]

    def frames(self) -> list[Frame]:
        return [f for _, f in sorted(self._frames.items())]


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------

_FRAME_ROWS_SQL = """
    SELECT family, value, schema_uri, introduced, deprecated, notes, aliases,
           parent
      FROM vocabulary_entries
     WHERE family = $1
       AND deprecated IS NULL
     ORDER BY value
"""


def _schema_uri(raw: Any) -> str:
    """The table defaults to the Iglu form; the model wants the bare one
    (the vocabulary cache's own normalisation, restated for a leaf)."""
    uri = str(raw or "") or "legba/vocabulary/1.0.0"
    if uri.startswith("iglu:"):
        try:
            major, minor, patch = uri.split("/jsonschema/", 1)[1].split("-")
            return f"legba/vocabulary/{major}.{minor}.{patch}"
        except Exception:  # noqa: BLE001 — a malformed URI is the default
            return "legba/vocabulary/1.0.0"
    return uri


def _entry(row: Any) -> VocabularyEntry:
    return VocabularyEntry(
        family=row["family"],
        value=row["value"],
        schema_uri=_schema_uri(row["schema_uri"]),
        introduced=row["introduced"] or datetime.fromtimestamp(0, tz=timezone.utc),
        deprecated=row["deprecated"],
        notes=row["notes"],
        aliases=[str(a) for a in (row["aliases"] or [])],
        parent=row["parent"],
    )


def _rule_errors(entry: VocabularyEntry) -> list[str]:
    bad = []
    for sel in entry.aliases:
        head, sep, val = str(sel).partition(":")
        if not sep or head not in MEMBER_SELECTORS or not val.strip():
            bad.append(str(sel))
    return bad


async def load_frame_catalog(conn: Any) -> FrameCatalog:
    """Read the ``frame`` family. ONE query on ``vocabulary_entries``.

    Degrades PER FAMILY, never raises: ``frame`` refused as a family, a row
    that fails the entry model, or a member selector outside
    :data:`MEMBER_SELECTORS` leaves the WHOLE family unloaded (a partly
    loaded frame set would resolve a partial roster, which is worse than the
    legacy rule); no rows is ``empty``. Either way the caller falls back.
    """
    try:
        validate_family(FRAME_FAMILY)
    except FamilyError as exc:
        return FrameCatalog.unloaded(exc.code)
    rows = await conn.fetch(_FRAME_ROWS_SQL, FRAME_FAMILY)
    entries: list[VocabularyEntry] = []
    for row in rows:
        try:
            entry = _entry(row)
        except Exception as exc:  # noqa: BLE001 — one bad row unloads the family
            logger.warning("frame family unloaded: row %r: %s", row["value"],
                           str(exc).splitlines()[0][:200])
            return FrameCatalog.unloaded(f"row {row['value']!r} invalid")
        bad = _rule_errors(entry)
        if bad:
            logger.warning("frame family unloaded: %r has member selectors %s",
                           entry.value, bad)
            return FrameCatalog.unloaded(f"row {entry.value!r} member rule {bad}")
        entries.append(entry)
    if not entries:
        return FrameCatalog((), status=STATUS_EMPTY)
    return FrameCatalog(entries)


# ---------------------------------------------------------------------------
# Resolve
# ---------------------------------------------------------------------------

#: The LEGACY member read — the id-as-tag idiom, verbatim the composition
#: composition slice's legacy member read (a test pins the two equal).
_LEGACY_MEMBER_ROSTER_SQL = """
    SELECT descriptor_id, name
      FROM target_descriptors
     WHERE is_head = TRUE
       AND state = 'active'
       AND descriptor_id <> $1
       AND (body -> 'scope' -> 'tags') ? $1
     ORDER BY descriptor_id
"""

#: The LEGACY roster read — verbatim the composition slice's roster read.
_LEGACY_ROSTER_SQL = """
    SELECT descriptor_id, name
      FROM target_descriptors
     WHERE is_head = TRUE
       AND state = 'active'
       AND (body -> 'scope' -> 'tags') ? $1
     ORDER BY descriptor_id
"""

#: The REGISTERED member read: any of the subtree's member tags, no frame of
#: the subtree itself. ``?|`` over the same JSONB the legacy ``?`` reads.
_MEMBER_ROSTER_SQL = """
    SELECT descriptor_id, name
      FROM target_descriptors
     WHERE is_head = TRUE
       AND state = 'active'
       AND (body -> 'scope' -> 'tags') ?| $1::text[]
       AND descriptor_id <> ALL($2::text[])
     ORDER BY descriptor_id
"""

#: A registered frame's own head (its name, and whether it is active).
_FRAME_HEADS_SQL = """
    SELECT descriptor_id, name, state
      FROM target_descriptors
     WHERE is_head = TRUE
       AND descriptor_id = ANY($1::text[])
"""


def composite_frame(
    frame_id: Any,
    catalog: FrameCatalog | None = None,
    *,
    legacy: LegacyFrames | None = None,
) -> Frame | None:
    """The frame ``frame_id`` names, or ``None`` when it names none.

    A loaded catalog is authoritative (an unregistered id is no frame). An
    unloaded / absent one falls back to ``legacy`` when given.
    """
    if catalog is not None and catalog.loaded:
        return catalog.composite_frame(frame_id)
    return legacy.frame(frame_id) if legacy is not None else None


def _named(rows: Sequence[Any]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for r in rows:
        did = str(r["descriptor_id"])
        name = r["name"]
        out.append({"target_id": did, "target_name": str(name) if name else did})
    return out


async def resolve_member_roster(
    frame_id: str,
    conn: Any,
    *,
    catalog: FrameCatalog | None = None,
    legacy: LegacyFrames | None = None,
) -> list[dict[str, str]]:
    """``[{target_id, target_name}]`` — the frame's member target heads, by id.

    Registered: the union of the member tags over the frame's SUBTREE (the
    frame and every frame whose parent walk reaches it), every subtree frame
    id excluded. Unloaded: the legacy id-as-tag read. A frame with no tag
    selector (an analyst lane, an unregistered id) has no target members —
    an honest empty, no query.
    """
    fid = str(frame_id)
    if catalog is None:
        catalog = await load_frame_catalog(conn)
    if not catalog.loaded:
        if legacy is None or legacy.frame(fid) is None:
            return []
        return _named(await conn.fetch(_LEGACY_MEMBER_ROSTER_SQL, fid))
    subtree = catalog.subtree(fid)
    tags = sorted({t for f in subtree for t in f.member_tags})
    if not tags:
        if catalog.composite_frame(fid) is None:
            logger.warning("frame %r is not registered in the frame family", fid)
        return []
    exclude = sorted({f.id for f in subtree})
    return _named(await conn.fetch(_MEMBER_ROSTER_SQL, tags, exclude))


async def resolve_members(
    frame_id: str,
    conn: Any,
    *,
    catalog: FrameCatalog | None = None,
    legacy: LegacyFrames | None = None,
) -> list[str]:
    """The frame's member target ids, by id (see :func:`resolve_member_roster`)."""
    return [
        m["target_id"]
        for m in await resolve_member_roster(frame_id, conn, catalog=catalog, legacy=legacy)
    ]


async def resolve_frame_roster(
    kind: str,
    conn: Any,
    *,
    catalog: FrameCatalog | None = None,
    legacy: LegacyFrames | None = None,
) -> list[dict[str, str]]:
    """``[{frame_id, frame_name}]`` — the subject groups of ``kind``, by id.

    Registered: :meth:`FrameCatalog.groups` that bind TARGETS (a ``tag:``
    selector; an analyst lane is a group the caller already reads off its
    own declaration). A frame whose head exists but is not active is left
    out, as the legacy read leaves it out; a frame with no head at all is a
    frame of the registry alone and is named by its label. Unloaded: the
    legacy roster-tag read.
    """
    if catalog is None:
        catalog = await load_frame_catalog(conn)
    if not catalog.loaded:
        if legacy is None:
            return []
        return [
            {"frame_id": m["target_id"], "frame_name": m["target_name"]}
            for m in _named(await conn.fetch(_LEGACY_ROSTER_SQL, legacy.kind))
        ]
    groups = [f for f in catalog.groups(kind) if f.member_tags]
    if not groups:
        return []
    heads = {
        str(r["descriptor_id"]): r
        for r in await conn.fetch(_FRAME_HEADS_SQL, [f.id for f in groups])
    }
    out: list[dict[str, str]] = []
    for f in groups:
        head = heads.get(f.id)
        if head is None:
            out.append({"frame_id": f.id, "frame_name": f.label})
        elif str(head["state"]) == "active":
            name = head["name"]
            out.append({"frame_id": f.id, "frame_name": str(name) if name else f.id})
    return out


__all__ = [
    "FRAME_FAMILY",
    "FRAME_FAMILY_ENV",
    "MEMBER_ANALYST",
    "MEMBER_SELECTORS",
    "MEMBER_TAG",
    "STATUS_EMPTY",
    "STATUS_LOADED",
    "STATUS_UNLOADED",
    "Frame",
    "FrameCatalog",
    "LegacyFrames",
    "composite_frame",
    "frame_family_enabled",
    "load_frame_catalog",
    "resolve_frame_roster",
    "resolve_member_roster",
    "resolve_members",
]
