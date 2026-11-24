# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Frame-kind awareness for the target registrars (W10 TAX-2).

The target registrars (``bringup_register_{g20_country,watch_country,region,
situation}_targets.py``) used to carry the frame topology as literals: the
region list, the ``region`` kind tag, the ``g20`` / ``watch`` roster tags. The
platform now registers that topology — the ``frame`` vocabulary family
(``legba.data.frames``, W7 FRAME-1: a frame is an entry whose ``parent`` is
``frame:<kind>``) and the profile's rosters (``legba.data.rosters``, ROSTER-1/2).
This module is what the registrars read instead:

* :func:`frame_kind_tags` — a target head that IS a registered frame of kind
  ``<kind>`` (its id is a ``frame`` entry whose parent is ``frame:<kind>``) is
  tagged ``frame:<kind>`` beside its legacy tags. Only when the frame family is
  LIVE: ``LEGBA_FRAME_FAMILY`` on AND the catalog loaded. Off (the default) —
  or unloaded, or the target not a frame — the legacy tag list comes back
  UNCHANGED, so every body is byte-identical to the pre-TAX-2 one.
* :func:`frame_groups` — the frames under a kind, read from the family (the
  region registrar's frame list when the family is live).
* :func:`roster_tag` — a desk's roster tag, refused BY NAME when the selected
  profile's ``rosters.subject_tags`` does not carry it (a registrar must not
  mint desks a profile's composition would never count).

Catalog sources: the registry (``GET /vocabulary/frame``, the REST
registrars), the database (``frames.load_frame_catalog``, the direct-DB
registrars) or the committed tree (``descriptors/vocabulary_frame.yaml``, a
``--dry-run``). Every one degrades to an UNLOADED catalog — never a partial
one — and an unloaded catalog changes nothing.
"""
from __future__ import annotations

import pathlib
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

import yaml

from legba.data import rosters
from legba.data.frames import (
    FRAME_FAMILY,
    Frame,
    FrameCatalog,
    frame_family_enabled,
    load_frame_catalog,
)
from legba.data.schemas.vocabulary import VocabularyEntry

#: The committed ``frame`` family (the walker seeds it).
FRAME_TREE = pathlib.Path(__file__).resolve().parent.parent / "descriptors" / "vocabulary_frame.yaml"

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def frame_tag(kind: str) -> str:
    """The tag a frame head of ``kind`` carries: ``frame:<kind>``."""
    return f"{FRAME_FAMILY}:{kind}"


def catalog_from_rows(rows: Iterable[Any]) -> FrameCatalog:
    """A catalog from entry mappings (a registry response, a tree file).
    One row that does not validate unloads the whole family."""
    entries: list[VocabularyEntry] = []
    for r in rows or ():
        if not isinstance(r, dict):
            return FrameCatalog.unloaded("non-mapping row")
        try:
            entries.append(VocabularyEntry.model_validate({
                "family": r.get("family", FRAME_FAMILY),
                "value": r.get("value"),
                "schema_uri": r.get("schema_uri") or "legba/vocabulary/1.0.0",
                "introduced": r.get("introduced") or _EPOCH,
                "deprecated": r.get("deprecated"),
                "notes": r.get("notes"),
                "aliases": list(r.get("aliases") or []),
                "parent": r.get("parent"),
            }, strict=False))
        except Exception as exc:  # noqa: BLE001 — one bad row unloads the family
            return FrameCatalog.unloaded(f"row {r.get('value')!r}: {str(exc).splitlines()[0][:120]}")
    if not entries:
        return FrameCatalog((), status="empty")
    return FrameCatalog(entries)


def tree_catalog(path: pathlib.Path = FRAME_TREE) -> FrameCatalog:
    """The committed family (``descriptors/vocabulary_frame.yaml``)."""
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        return FrameCatalog.unloaded(f"{path.name}: {exc}")
    return catalog_from_rows(
        {**e, "family": doc.get("family", FRAME_FAMILY)} for e in doc.get("entries") or ()
    )


def catalog_from_registry(client: Any) -> FrameCatalog:
    """The registered family, read through a registry REST client."""
    try:
        r = client.get(f"/vocabulary/{FRAME_FAMILY}")
    except Exception as exc:  # noqa: BLE001
        return FrameCatalog.unloaded(f"registry read failed: {exc}")
    if r.status_code != 200 or not isinstance(r.json(), list):
        return FrameCatalog.unloaded(f"registry read returned {r.status_code}")
    return catalog_from_rows(r.json())


async def catalog_from_db(pg: Any) -> FrameCatalog:
    """The registered family, read from the database (one query)."""
    async with pg.acquire() as conn:
        return await load_frame_catalog(conn)


def family_live(catalog: FrameCatalog | None, *, enabled: bool | None = None) -> bool:
    """The flag is on AND the catalog is loaded."""
    on = frame_family_enabled() if enabled is None else enabled
    return bool(on and catalog is not None and catalog.loaded)


def frame_kind_of(target_id: str, catalog: FrameCatalog) -> str | None:
    """The kind of the frame ``target_id`` IS, or ``None`` when it is none."""
    frame = catalog.composite_frame(target_id)
    return frame.parent_id if frame is not None else None


def frame_kind_tags(
    target_id: str,
    legacy_tags: Sequence[str],
    catalog: FrameCatalog | None,
    *,
    enabled: bool | None = None,
) -> list[str]:
    """``legacy_tags`` plus ``frame:<kind>`` when the family is live and
    ``target_id`` is a registered frame of ``<kind>``; else ``legacy_tags``
    unchanged (a new list, same items, same order)."""
    if not family_live(catalog, enabled=enabled):
        return list(legacy_tags)
    kind = frame_kind_of(target_id, catalog)
    if kind is None:
        return list(legacy_tags)
    return list(dict.fromkeys([*legacy_tags, frame_tag(kind)]))


def frame_groups(kind: str, catalog: FrameCatalog) -> list[Frame]:
    """The frames under ``kind`` that carry a member rule, by id."""
    return catalog.groups(kind) if catalog is not None and catalog.loaded else []


def roster_tag(tag: str) -> str:
    """``tag`` when the selected profile's subject roster carries it; else a
    ``ValueError`` naming the tag and the roster."""
    roster = rosters.subject_roster_tags()
    if tag not in roster:
        raise ValueError(
            f"roster tag {tag!r} is not on the profile's subject roster "
            f"{list(roster)} — a desk tagged {tag!r} would never be counted"
        )
    return tag


__all__ = [
    "FRAME_TREE",
    "catalog_from_db",
    "catalog_from_registry",
    "catalog_from_rows",
    "family_live",
    "frame_family_enabled",
    "frame_groups",
    "frame_kind_of",
    "frame_kind_tags",
    "frame_tag",
    "roster_tag",
    "tree_catalog",
]
