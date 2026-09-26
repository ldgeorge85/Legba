# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Program 6 L0 — the idempotent upsert of one curated ``layer_map`` into
``source_layers`` / ``desk_apertures`` (migration 0214).

THE ONE WRITER. ``scripts/load_layer_map.py`` is the only caller in this lane
— no analyst, no HTTP route, no registry-lifecycle registration reads or
writes this module. Kept as a package function rather than inline in the
script so a future writer (a registry route, if Program 6 ever earns one)
reuses this contract instead of re-deriving it, the same split
``scripts/load_unit_reference.py`` takes with
``legba.data.analysts.deterministic_handlers._reference_store``.

WHY THE DESCRIPTOR CARRIES NO ``target_id``. A country's layer TABLE
(``source_layers``) is a fact about the country's information environment;
which DESK reads it is a separate fact this module refuses to guess — a
country could in principle back more than one desk, and the country ->
target_id resolution belongs to ``runtime/target_resolution.py``'s domain
(``country_watch_<cc>`` / ``country_g20_<cc>``), not to this migration or this
loader. The caller supplies ``target_id`` explicitly for the aperture half of
the load.

IDEMPOTENCE + RE-VERSIONING (the ``entity_edges`` shape, migration 0143). Every
row is open (``valid_until IS NULL``) or closed history; nothing is UPDATEd in
place. For each (source_id, country) / (target_id, layer) key:

  * no open row exists           -> INSERT a new open row.
  * an open row exists at the SAME ``map_version``
    - identical content         -> no-op (this is what "re-loading the same
                                    version changes nothing" means).
    - DIFFERENT content         -> refuse loudly. A file whose content moved
                                    under an unchanged version number is an
                                    operator error (bump ``map_version``), not
                                    a case to silently reconcile — silently
                                    picking a winner would be exactly the kind
                                    of guess this codebase refuses to make.
  * an open row exists at a DIFFERENT ``map_version``
                                 -> close it (``valid_until = now()``) and
                                    INSERT the new row. This runs even when the
                                    content is byte-identical to the prior
                                    version: the map is versioned as a whole,
                                    not row-by-row, so a version bump always
                                    supersedes.

The whole load runs inside one transaction the caller manages, so a partial
load (some rows loaded, others rejected by the same-version-different-content
guard) never lands.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from ..registry.layer_map_schema import LayerMapDescriptor


class LayerMapVersionConflict(ValueError):
    """An open row already carries this exact ``map_version`` with DIFFERENT
    content — the file changed without a version bump. Refused rather than
    silently applied; the fix is to mint a new ``map_version``."""


class _Connection(Protocol):
    async def fetchrow(self, query: str, *args: Any) -> Any: ...
    async def execute(self, query: str, *args: Any) -> Any: ...


@dataclass
class LoadReceipt:
    inserted: int = 0
    superseded: int = 0
    unchanged: int = 0
    kind_counts: dict[str, int] = field(default_factory=dict)

    def bump(self, kind: str, outcome: str) -> None:
        setattr(self, outcome, getattr(self, outcome) + 1)
        self.kind_counts[f"{kind}.{outcome}"] = (
            self.kind_counts.get(f"{kind}.{outcome}", 0) + 1
        )


_SELECT_OPEN_LAYER = """
    SELECT id, layer, reason, map_version
      FROM public.source_layers
     WHERE source_id = $1 AND country = $2 AND valid_until IS NULL
"""
_CLOSE_LAYER = "UPDATE public.source_layers SET valid_until = now() WHERE id = $1"
_INSERT_LAYER = """
    INSERT INTO public.source_layers
        (source_id, country, layer, reason, map_version)
    VALUES ($1, $2, $3, $4, $5)
"""

_SELECT_OPEN_APERTURE = """
    SELECT id, declared, reason, map_version
      FROM public.desk_apertures
     WHERE target_id = $1 AND layer = $2 AND valid_until IS NULL
"""
_CLOSE_APERTURE = "UPDATE public.desk_apertures SET valid_until = now() WHERE id = $1"
_INSERT_APERTURE = """
    INSERT INTO public.desk_apertures
        (target_id, layer, declared, reason, map_version)
    VALUES ($1, $2, $3, $4, $5)
"""


async def _upsert_one(
    conn: _Connection,
    receipt: LoadReceipt,
    kind: str,
    *,
    select_sql: str,
    select_args: tuple[Any, ...],
    close_sql: str,
    insert_sql: str,
    insert_args: tuple[Any, ...],
    new_content: tuple[Any, ...],
    content_fields: tuple[str, ...],
    identity_desc: str,
) -> None:
    row = await conn.fetchrow(select_sql, *select_args)
    if row is None:
        await conn.execute(insert_sql, *insert_args)
        receipt.bump(kind, "inserted")
        return

    existing_content = tuple(row[f] for f in content_fields)
    if row["map_version"] == insert_args[-1]:
        if existing_content == new_content:
            receipt.bump(kind, "unchanged")
            return
        raise LayerMapVersionConflict(
            f"{identity_desc}: map_version {row['map_version']!r} is already "
            f"loaded with different content ({content_fields}: "
            f"{existing_content!r} on the open row vs {new_content!r} in this "
            "file). Bump map_version rather than reloading the same version "
            "with different content."
        )

    # A different (older) open version — supersede it, then open the new row.
    await conn.execute(close_sql, row["id"])
    await conn.execute(insert_sql, *insert_args)
    receipt.bump(kind, "superseded")


async def load_layer_map(
    conn: _Connection, descriptor: LayerMapDescriptor, *, target_id: str
) -> LoadReceipt:
    """Upsert one validated :class:`LayerMapDescriptor` into ``source_layers``
    (keyed by ``descriptor.country``) and ``desk_apertures`` (keyed by the
    caller-supplied ``target_id``). Call inside a transaction — see the module
    docstring."""
    receipt = LoadReceipt()
    country = descriptor.country
    version = descriptor.map_version

    for entry in descriptor.entries:
        await _upsert_one(
            conn,
            receipt,
            "source_layers",
            select_sql=_SELECT_OPEN_LAYER,
            select_args=(entry.source_id, country),
            close_sql=_CLOSE_LAYER,
            insert_sql=_INSERT_LAYER,
            insert_args=(entry.source_id, country, entry.layer, entry.reason, version),
            new_content=(entry.layer, entry.reason),
            content_fields=("layer", "reason"),
            identity_desc=f"source_layers[{entry.source_id}/{country}]",
        )

    for aperture in descriptor.apertures:
        await _upsert_one(
            conn,
            receipt,
            "desk_apertures",
            select_sql=_SELECT_OPEN_APERTURE,
            select_args=(target_id, aperture.layer),
            close_sql=_CLOSE_APERTURE,
            insert_sql=_INSERT_APERTURE,
            insert_args=(
                target_id, aperture.layer, aperture.declared, aperture.reason, version,
            ),
            new_content=(aperture.declared, aperture.reason),
            content_fields=("declared", "reason"),
            identity_desc=f"desk_apertures[{target_id}/{aperture.layer}]",
        )

    return receipt


__all__ = ["LayerMapVersionConflict", "LoadReceipt", "load_layer_map"]
