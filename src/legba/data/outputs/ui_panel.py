# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""L-192 UI panel output kind — descriptor-driven panel registration.

This is the *registration* surface that the L-204 UI rebuild will consume.
Real React/Dockview panels do not live here — they land in `legba-ui/src/
panels/v3/` once L-204 starts. This module's only job is to hold the data
side: when a descriptor declares ``outputs: [{ kind: ui_panel, config:
{...} }]`` and lands in the registry, walk each ``ui_panel`` entry and
materialize a row in ``ui_panel_registrations`` so the frontend can read
it back at boot + watch its NATS events.

Per L-108 §1 the model is two cooperating registries:

  * **Bundle-time panel registry** (frontend, static). Lists panel
    *kinds* — `target.overview`, `analyst.runs`, etc. Not this module.

  * **Runtime descriptor registry** (backend, dynamic). Lists which
    *instances* exist. Per L-108 §8 step [4], a NATS subscriber on the
    frontend reacts to ``registry.bindings.activated`` /
    ``registry.bindings.retired`` events sourced from this table.

This module owns the second registry's *persistence + lookup* surface.

Descriptor shape (per L-108 §3, §4):

    outputs:
      - kind: ui_panel
        config:
          panel: panels.target_overview        # logical panel-id (required)
          binding:
            target_id: "{self.id}"             # resolved scope
          mode: personal                       # personal | above_ai | cis
          layout_slot: dashboard.intelligence.india
          title: "India — Energy Overview"
          data_query:                          # opaque to registry; L-204 interprets
            kind: rest
            path: /api/v3/targets/india

The L-192 brief locks **mode-conditional visibility as a column** (not
runtime JSON filtering) and pushes layout-slot conflict detection into
the SQL layer via a partial unique index — both for L-204 query
ergonomics. See migration ``0017_ui_panel_registrations.sql``.

Contract surface
----------------

  * ``KIND_NAME = "ui_panel"`` — registry discovery.
  * :class:`UIPanelRegistry` — async CRUD over ``ui_panel_registrations``.
  * :class:`PanelRegistration` — the row dataclass.
  * :func:`register_from_descriptor` — descriptor → list[row] materializer.

Failure modes
-------------

  * Missing required field (``panel``, ``mode``, ``layout_slot``): raises
    :class:`UIPanelDescriptorError` synchronously. Programmer error.
  * Layout-slot collision within the same mode for two different active
    panels: raises :class:`LayoutSlotConflict`. The unique index in the
    migration is the belt; the registry catches it cleanly via a SELECT
    pre-flight so the error message names the conflicting panel.
  * Unknown mode literal: raises :class:`UIPanelDescriptorError`.

The kind exposes NO ``emit`` callable — the registry is the surface.
``discover_output_kinds`` treats this kind the same way it treats
``substrate`` and ``mcp_tool`` (KIND_NAME present, ``emit`` absent).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Literal, Mapping
from uuid import UUID

import asyncpg

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Kind identity (host registry hook)
# ---------------------------------------------------------------------------


KIND_NAME: str = "ui_panel"


# ---------------------------------------------------------------------------
# Mode literal
# ---------------------------------------------------------------------------


# Per L-108 §9 (modes) + M-036 deployment-mode taxonomy. The wire form
# stored in the DB is snake_case so SQL grammar is friendly; the dataclass
# exposes the canonical strings the descriptor authors write.
PanelMode = Literal["personal", "above_ai", "cis"]

_ALLOWED_MODES: frozenset[str] = frozenset({"personal", "above_ai", "cis"})

# Aliases the descriptor schema may produce — L-108 mixes `above-ai`,
# `cis_fellowship`, etc. We normalize on the registry boundary so the SQL
# enum stays compact and the L-204 mode-filter is a single WHERE clause.
_MODE_ALIASES: Mapping[str, str] = {
    "personal": "personal",
    "above-ai": "above_ai",
    "above_ai": "above_ai",
    "aboveai": "above_ai",
    "cis": "cis",
    "cis-fellowship": "cis",
    "cis_fellowship": "cis",
}


def _normalize_mode(raw: str) -> str:
    """Canonicalize a mode literal. Raises on unknown values."""
    if not isinstance(raw, str) or not raw:
        raise UIPanelDescriptorError(
            f"ui_panel mode must be a non-empty string (got {raw!r})"
        )
    key = raw.strip().lower()
    canonical = _MODE_ALIASES.get(key)
    if canonical is None:
        raise UIPanelDescriptorError(
            f"unknown ui_panel mode {raw!r}; expected one of: "
            f"{sorted(_ALLOWED_MODES)} (aliases accepted: "
            f"{sorted(_MODE_ALIASES)})"
        )
    return canonical


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class UIPanelDescriptorError(ValueError):
    """Descriptor declared a malformed ``outputs.ui_panel`` entry."""


class LayoutSlotConflict(RuntimeError):
    """Two active panels collide on the same (mode, layout_slot) tuple.

    The L-204 layout-preset expansion needs slot uniqueness within a mode
    so a preset reference (`legba-target-default → dashboard.intelligence.
    brazil`) resolves to exactly one panel. We catch the collision at
    registration time so operators see the conflict before the SQL UNIQUE
    index trips with an opaque error.
    """

    def __init__(
        self,
        *,
        mode: str,
        layout_slot: str,
        existing_panel_id: str,
        attempted_panel_id: str,
    ) -> None:
        self.mode = mode
        self.layout_slot = layout_slot
        self.existing_panel_id = existing_panel_id
        self.attempted_panel_id = attempted_panel_id
        super().__init__(
            f"layout_slot {layout_slot!r} (mode={mode!r}) already held by "
            f"active panel {existing_panel_id!r}; cannot register "
            f"{attempted_panel_id!r}"
        )


# ---------------------------------------------------------------------------
# PanelRegistration dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PanelRegistration:
    """One row in ``ui_panel_registrations``.

    Fields mirror the migration column-set 1:1 so callers can round-trip
    a row through Postgres without a separate ORM layer. ``id`` and
    ``created_at`` are populated by the DB; ``register_from_descriptor``
    inserts with ``id=None`` and reads them back.

    Attributes
    ----------
    id:
        Surrogate primary key. ``None`` before insert.
    panel_id:
        Stable logical id referenced by descriptors as
        ``panels.<panel_id>``. Example: ``"target_overview"``.
    descriptor_id:
        Owning descriptor's id (target or analyst).
    descriptor_version:
        Content-hash version of the owning descriptor at registration
        time. The L-204 frontend uses this to invalidate react-query
        caches on descriptor updates (see L-108 §10).
    descriptor_family:
        ``"target"`` | ``"analyst"`` — which descriptor table the row
        descends from.
    analyst_id:
        Convenience for analyst-driven panels: ``descriptor_id`` when
        ``descriptor_family == "analyst"``, else ``None``. Lets the
        future UI rebuild JOIN against ``analyst_descriptors`` without
        parsing ``descriptor_family``.
    title:
        Human-readable panel title (may interpolate scope on render).
    mode:
        Canonical mode string. One of ``"personal" | "above_ai" | "cis"``.
        Aliases (e.g. ``"cis_fellowship"``) are accepted by
        :func:`register_from_descriptor` and normalized here.
    data_query:
        Free-form mapping describing the panel's data binding. Examples:
        ``{"kind": "rest", "path": "/api/v3/targets/india"}``,
        ``{"kind": "nats", "subject": "analyst.*.state.india"}``,
        ``{"kind": "sql", "query": "SELECT ..."}``. The registry stores
        the value as JSONB and does not interpret it; the L-204
        ``dataSources`` router does.
    layout_slot:
        L-092 layout-preset slot identifier. Example:
        ``"dashboard.intelligence.india"``. Unique per (mode,
        layout_slot) among non-retired rows.
    binding:
        Resolved scope mapping (e.g. ``{"target_id": "brazil"}``).
        Empty dict for singleton panels.
    retired:
        Soft-delete flag. ``True`` after the owning descriptor retires;
        the row stays so L-204 layout restores can show an
        UnboundPanelPlaceholder.
    created_at:
        DB-stamped insert time. ``None`` before insert.
    retired_at:
        DB-stamped retirement time. ``None`` while ``retired == False``.
    """

    panel_id: str
    descriptor_id: str
    descriptor_version: str
    descriptor_family: str
    title: str
    mode: str
    layout_slot: str
    data_query: Mapping[str, Any] = field(default_factory=dict)
    binding: Mapping[str, Any] = field(default_factory=dict)
    analyst_id: str | None = None
    id: UUID | None = None
    retired: bool = False
    created_at: datetime | None = None
    retired_at: datetime | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "PanelRegistration":
        """Build a registration from an asyncpg row mapping."""
        return cls(
            id=row["id"],
            panel_id=row["panel_id"],
            descriptor_id=row["descriptor_id"],
            descriptor_version=row["descriptor_version"],
            descriptor_family=row["descriptor_family"],
            analyst_id=row["analyst_id"],
            title=row["title"],
            mode=row["mode"],
            data_query=_json_load(row["data_query"]) or {},
            layout_slot=row["layout_slot"],
            binding=_json_load(row["binding"]) or {},
            retired=row["retired"],
            created_at=row["created_at"],
            retired_at=row["retired_at"],
        )


def _json_load(value: Any) -> Any:
    """asyncpg returns JSONB as str (by default codec) or dict (if codec
    registered). Tolerate both so this module works whether or not the
    caller registered the jsonb codec."""
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    if isinstance(value, str):
        if not value:
            return None
        return json.loads(value)
    return value


# ---------------------------------------------------------------------------
# Descriptor parsing
# ---------------------------------------------------------------------------


def _required_str(config: Mapping[str, Any], key: str, *, panel_idx: int) -> str:
    val = config.get(key)
    if not isinstance(val, str) or not val:
        raise UIPanelDescriptorError(
            f"outputs[{panel_idx}].config.{key} is required and must be a "
            f"non-empty string (got {val!r})"
        )
    return val


def _parse_panel_id(raw: str, *, panel_idx: int) -> str:
    """Accept either ``panels.<panel_id>`` or the bare ``<panel_id>``.

    L-108 §4 standard form is ``panel: panels.target_overview``; we strip
    the prefix so the stored panel_id is the bare logical name. Operators
    who write just ``target_overview`` round-trip cleanly.
    """
    name = raw.strip()
    if name.startswith("panels."):
        name = name[len("panels."):]
    if not name:
        raise UIPanelDescriptorError(
            f"outputs[{panel_idx}].config.panel resolved to empty panel_id "
            f"from {raw!r}"
        )
    # Panel ids share the descriptor-id grammar (snake_case-ish). We don't
    # enforce the full regex here — the frontend bundle-time registry is
    # the source of truth for valid kinds; we just check no whitespace /
    # dots leak through.
    for bad in (" ", "\t", "\n", ".", "/", "\\"):
        if bad in name:
            raise UIPanelDescriptorError(
                f"outputs[{panel_idx}].config.panel contains illegal char "
                f"{bad!r}: {name!r}"
            )
    return name


def _parse_binding(config: Mapping[str, Any], *, panel_idx: int) -> dict[str, Any]:
    binding = config.get("binding", {})
    if binding is None:
        return {}
    if not isinstance(binding, Mapping):
        raise UIPanelDescriptorError(
            f"outputs[{panel_idx}].config.binding must be a mapping "
            f"(got {type(binding).__name__})"
        )
    return dict(binding)


def _parse_data_query(config: Mapping[str, Any], *, panel_idx: int) -> dict[str, Any]:
    data_query = config.get("data_query", {})
    if data_query is None:
        return {}
    if isinstance(data_query, str):
        # Bare-string shorthand — promote to {"subject": ...} since the
        # most common single-string form in the spec is a NATS subject.
        return {"subject": data_query}
    if not isinstance(data_query, Mapping):
        raise UIPanelDescriptorError(
            f"outputs[{panel_idx}].config.data_query must be a mapping or "
            f"string (got {type(data_query).__name__})"
        )
    return dict(data_query)


def _parse_one_entry(
    entry: Mapping[str, Any],
    *,
    panel_idx: int,
    descriptor_id: str,
    descriptor_version: str,
    descriptor_family: str,
) -> PanelRegistration:
    """Parse one ``outputs[i]`` entry whose ``kind == "ui_panel"`` into a
    :class:`PanelRegistration` (id / created_at unset, pre-insert)."""
    if entry.get("kind") != KIND_NAME:
        raise UIPanelDescriptorError(
            f"outputs[{panel_idx}].kind must be {KIND_NAME!r} "
            f"(got {entry.get('kind')!r})"
        )

    config = entry.get("config", {})
    if not isinstance(config, Mapping):
        raise UIPanelDescriptorError(
            f"outputs[{panel_idx}].config must be a mapping "
            f"(got {type(config).__name__})"
        )

    panel_raw = _required_str(config, "panel", panel_idx=panel_idx)
    panel_id = _parse_panel_id(panel_raw, panel_idx=panel_idx)
    mode = _normalize_mode(_required_str(config, "mode", panel_idx=panel_idx))
    layout_slot = _required_str(config, "layout_slot", panel_idx=panel_idx)
    title = (
        config.get("title")
        if isinstance(config.get("title"), str) and config.get("title")
        else f"{descriptor_id} — {panel_id}"
    )
    binding = _parse_binding(config, panel_idx=panel_idx)
    data_query = _parse_data_query(config, panel_idx=panel_idx)

    analyst_id: str | None = None
    if descriptor_family == "analyst":
        analyst_id = descriptor_id

    return PanelRegistration(
        panel_id=panel_id,
        descriptor_id=descriptor_id,
        descriptor_version=descriptor_version,
        descriptor_family=descriptor_family,
        analyst_id=analyst_id,
        title=title,
        mode=mode,
        data_query=data_query,
        layout_slot=layout_slot,
        binding=binding,
    )


# ---------------------------------------------------------------------------
# UIPanelRegistry — async CRUD
# ---------------------------------------------------------------------------


class UIPanelRegistry:
    """Async CRUD surface over ``ui_panel_registrations``.

    All methods accept an :class:`asyncpg.Connection`-shaped object; the
    caller owns connection / pool / transaction lifecycle. This matches
    the Wave A output-kind pattern (see ``substrate.py``).

    The registry persists exactly what :class:`PanelRegistration`
    describes. It does **not**:

      * Validate that ``panel_id`` exists in the L-204 bundle-time
        registry — that's a frontend concern; an unknown id surfaces as
        an UnboundPanelPlaceholder on the React side.
      * Subscribe to descriptor-registry NATS events — the
        ``DescriptorRegistry`` (L-110) is expected to call
        :meth:`register` on activation and :meth:`retire_for_descriptor`
        on retirement. The NATS event the L-204 frontend listens to is
        emitted by the descriptor registry (see L-108 §8).
    """

    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    # ------------------------------------------------------------------
    # Write paths
    # ------------------------------------------------------------------

    async def register(self, reg: PanelRegistration) -> PanelRegistration:
        """Insert (or upsert on (descriptor_id, descriptor_version,
        panel_id)) one panel registration. Returns the row with ``id``
        and ``created_at`` populated.

        Raises :class:`LayoutSlotConflict` if a different active panel
        already holds ``(mode, layout_slot)``. The SQL UNIQUE index is
        the final guard; we pre-flight here so the error names the
        existing panel.
        """
        # Pre-flight slot-conflict check. We only care about *active*
        # rows belonging to a different descriptor or panel_id.
        existing = await self._conn.fetchrow(
            """
            SELECT panel_id, descriptor_id, descriptor_version
              FROM ui_panel_registrations
             WHERE mode = $1
               AND layout_slot = $2
               AND retired = FALSE
            """,
            reg.mode,
            reg.layout_slot,
        )
        if existing is not None:
            same_owner = (
                existing["descriptor_id"] == reg.descriptor_id
                and existing["descriptor_version"] == reg.descriptor_version
                and existing["panel_id"] == reg.panel_id
            )
            if not same_owner:
                raise LayoutSlotConflict(
                    mode=reg.mode,
                    layout_slot=reg.layout_slot,
                    existing_panel_id=existing["panel_id"],
                    attempted_panel_id=reg.panel_id,
                )

        row = await self._conn.fetchrow(
            """
            INSERT INTO ui_panel_registrations (
                panel_id, descriptor_id, descriptor_version,
                descriptor_family, analyst_id, title, mode,
                data_query, layout_slot, binding
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10::jsonb)
            ON CONFLICT (descriptor_id, descriptor_version, panel_id)
              DO UPDATE SET
                  title       = EXCLUDED.title,
                  mode        = EXCLUDED.mode,
                  data_query  = EXCLUDED.data_query,
                  layout_slot = EXCLUDED.layout_slot,
                  binding     = EXCLUDED.binding,
                  analyst_id  = EXCLUDED.analyst_id,
                  retired     = FALSE,
                  retired_at  = NULL
            RETURNING id, panel_id, descriptor_id, descriptor_version,
                      descriptor_family, analyst_id, title, mode,
                      data_query, layout_slot, binding,
                      retired, created_at, retired_at
            """,
            reg.panel_id,
            reg.descriptor_id,
            reg.descriptor_version,
            reg.descriptor_family,
            reg.analyst_id,
            reg.title,
            reg.mode,
            json.dumps(dict(reg.data_query)),
            reg.layout_slot,
            json.dumps(dict(reg.binding)),
        )
        logger.info(
            "ui_panel.register panel_id=%s descriptor=%s/%s mode=%s slot=%s",
            reg.panel_id,
            reg.descriptor_id,
            reg.descriptor_version[:12] if reg.descriptor_version else "",
            reg.mode,
            reg.layout_slot,
        )
        return PanelRegistration.from_row(row)

    async def retire(self, registration_id: UUID) -> bool:
        """Soft-delete a single registration by id. Returns ``True`` if
        the row transitioned from active → retired (idempotent: a
        no-op repeat returns ``False``)."""
        status = await self._conn.execute(
            """
            UPDATE ui_panel_registrations
               SET retired    = TRUE,
                   retired_at = NOW()
             WHERE id = $1
               AND retired = FALSE
            """,
            registration_id,
        )
        # asyncpg returns "UPDATE <n>"; split on whitespace to read n.
        try:
            n = int(status.rsplit(" ", 1)[-1])
        except ValueError:                                      # pragma: no cover
            n = 0
        return n > 0

    async def retire_for_descriptor(
        self,
        descriptor_id: str,
        *,
        descriptor_version: str | None = None,
    ) -> int:
        """Soft-delete every active panel registration owned by the
        named descriptor. Used by the descriptor registry on
        ``retire`` (and on update, against the *prior* version).

        If ``descriptor_version`` is supplied, only rows pinned to that
        version are retired — useful when a descriptor update fans out
        new panel rows and the prior version's rows should drop.

        Returns the number of rows retired.
        """
        if descriptor_version is None:
            status = await self._conn.execute(
                """
                UPDATE ui_panel_registrations
                   SET retired    = TRUE,
                       retired_at = NOW()
                 WHERE descriptor_id = $1
                   AND retired = FALSE
                """,
                descriptor_id,
            )
        else:
            status = await self._conn.execute(
                """
                UPDATE ui_panel_registrations
                   SET retired    = TRUE,
                       retired_at = NOW()
                 WHERE descriptor_id = $1
                   AND descriptor_version = $2
                   AND retired = FALSE
                """,
                descriptor_id,
                descriptor_version,
            )
        try:
            n = int(status.rsplit(" ", 1)[-1])
        except ValueError:                                      # pragma: no cover
            n = 0
        logger.info(
            "ui_panel.retire_for_descriptor descriptor=%s version=%s n=%d",
            descriptor_id,
            descriptor_version,
            n,
        )
        return n

    # ------------------------------------------------------------------
    # Read paths — L-204 consumes these
    # ------------------------------------------------------------------

    async def get(self, registration_id: UUID) -> PanelRegistration | None:
        row = await self._conn.fetchrow(
            """
            SELECT id, panel_id, descriptor_id, descriptor_version,
                   descriptor_family, analyst_id, title, mode,
                   data_query, layout_slot, binding,
                   retired, created_at, retired_at
              FROM ui_panel_registrations
             WHERE id = $1
            """,
            registration_id,
        )
        return PanelRegistration.from_row(row) if row else None

    async def list_by_mode(
        self,
        mode: str,
        *,
        include_retired: bool = False,
    ) -> list[PanelRegistration]:
        """Bundle-time hot path: every active panel registered for a
        given mode. The L-204 frontend calls this once at boot.
        """
        canonical = _normalize_mode(mode)
        if include_retired:
            rows = await self._conn.fetch(
                """
                SELECT id, panel_id, descriptor_id, descriptor_version,
                       descriptor_family, analyst_id, title, mode,
                       data_query, layout_slot, binding,
                       retired, created_at, retired_at
                  FROM ui_panel_registrations
                 WHERE mode = $1
                 ORDER BY layout_slot, created_at
                """,
                canonical,
            )
        else:
            rows = await self._conn.fetch(
                """
                SELECT id, panel_id, descriptor_id, descriptor_version,
                       descriptor_family, analyst_id, title, mode,
                       data_query, layout_slot, binding,
                       retired, created_at, retired_at
                  FROM ui_panel_registrations
                 WHERE mode = $1
                   AND retired = FALSE
                 ORDER BY layout_slot, created_at
                """,
                canonical,
            )
        return [PanelRegistration.from_row(r) for r in rows]

    async def list_by_layout_slot(
        self,
        layout_slot: str,
        *,
        include_retired: bool = False,
    ) -> list[PanelRegistration]:
        """Preset-resolution path. Returns every registration claiming
        the named slot. With ``include_retired=False`` (default) the
        unique index guarantees the result is at most one row per mode;
        with ``True`` the historical chain shows up so L-204 layout
        restore can render UnboundPanelPlaceholder for stale slots.
        """
        if include_retired:
            rows = await self._conn.fetch(
                """
                SELECT id, panel_id, descriptor_id, descriptor_version,
                       descriptor_family, analyst_id, title, mode,
                       data_query, layout_slot, binding,
                       retired, created_at, retired_at
                  FROM ui_panel_registrations
                 WHERE layout_slot = $1
                 ORDER BY retired, created_at DESC
                """,
                layout_slot,
            )
        else:
            rows = await self._conn.fetch(
                """
                SELECT id, panel_id, descriptor_id, descriptor_version,
                       descriptor_family, analyst_id, title, mode,
                       data_query, layout_slot, binding,
                       retired, created_at, retired_at
                  FROM ui_panel_registrations
                 WHERE layout_slot = $1
                   AND retired = FALSE
                 ORDER BY mode, created_at
                """,
                layout_slot,
            )
        return [PanelRegistration.from_row(r) for r in rows]

    async def list_for_descriptor(
        self,
        descriptor_id: str,
        *,
        descriptor_version: str | None = None,
        include_retired: bool = True,
    ) -> list[PanelRegistration]:
        """Every registration owned by the named descriptor. Default
        includes retired rows so an operator inspecting a retired
        descriptor still sees its panel history.
        """
        clauses = ["descriptor_id = $1"]
        params: list[Any] = [descriptor_id]
        if descriptor_version is not None:
            clauses.append(f"descriptor_version = ${len(params) + 1}")
            params.append(descriptor_version)
        if not include_retired:
            clauses.append("retired = FALSE")
        sql = (
            "SELECT id, panel_id, descriptor_id, descriptor_version, "
            "descriptor_family, analyst_id, title, mode, data_query, "
            "layout_slot, binding, retired, created_at, retired_at "
            "FROM ui_panel_registrations "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY panel_id"
        )
        rows = await self._conn.fetch(sql, *params)
        return [PanelRegistration.from_row(r) for r in rows]


# ---------------------------------------------------------------------------
# Descriptor → registry driver
# ---------------------------------------------------------------------------


def parse_descriptor_panels(
    *,
    descriptor_id: str,
    descriptor_version: str,
    descriptor_family: str,
    outputs: Iterable[Mapping[str, Any]],
) -> list[PanelRegistration]:
    """Pure parser — walk a descriptor's ``outputs`` block and return one
    :class:`PanelRegistration` per ``ui_panel`` entry, *without touching
    the database*. Useful for dry-run validators (the L-110 dead-letter
    path runs this before commit).
    """
    if descriptor_family not in ("target", "analyst"):
        raise UIPanelDescriptorError(
            f"descriptor_family must be 'target' or 'analyst' "
            f"(got {descriptor_family!r})"
        )
    out: list[PanelRegistration] = []
    seen_panel_ids: set[str] = set()
    for idx, entry in enumerate(outputs):
        if not isinstance(entry, Mapping):
            continue
        if entry.get("kind") != KIND_NAME:
            continue
        reg = _parse_one_entry(
            entry,
            panel_idx=idx,
            descriptor_id=descriptor_id,
            descriptor_version=descriptor_version,
            descriptor_family=descriptor_family,
        )
        if reg.panel_id in seen_panel_ids:
            raise UIPanelDescriptorError(
                f"descriptor {descriptor_id!r} declares panel "
                f"{reg.panel_id!r} more than once in outputs"
            )
        seen_panel_ids.add(reg.panel_id)
        out.append(reg)
    return out


async def register_from_descriptor(
    conn: asyncpg.Connection,
    *,
    descriptor_id: str,
    descriptor_version: str,
    descriptor_family: str,
    outputs: Iterable[Mapping[str, Any]],
    retire_prior_versions: bool = True,
) -> list[PanelRegistration]:
    """Walk a descriptor's ``outputs`` block, register every
    ``ui_panel`` entry, and return the persisted rows.

    Parameters
    ----------
    conn:
        Live asyncpg connection. The caller owns the surrounding
        transaction.
    descriptor_id, descriptor_version, descriptor_family:
        Identifying triple for the owning descriptor.
        ``descriptor_family`` must be ``"target"`` or ``"analyst"``.
    outputs:
        The descriptor's ``outputs`` list (each entry a mapping; only
        those with ``kind == "ui_panel"`` are consumed).
    retire_prior_versions:
        If ``True`` (default), retire any active rows owned by the same
        ``descriptor_id`` at a different ``descriptor_version`` before
        the new rows go in. This is the L-108 §10 "descriptor mutation"
        contract — old bindings emit ``registry.bindings.retired``,
        new bindings emit ``registry.bindings.activated``. The caller
        (typically L-110's descriptor registry) is responsible for
        emitting the NATS events; this function is the persistence
        half.

    Raises
    ------
    UIPanelDescriptorError:
        Any ``ui_panel`` entry is malformed.
    LayoutSlotConflict:
        A new entry's ``(mode, layout_slot)`` collides with another
        descriptor's active row.
    """
    parsed = parse_descriptor_panels(
        descriptor_id=descriptor_id,
        descriptor_version=descriptor_version,
        descriptor_family=descriptor_family,
        outputs=outputs,
    )

    registry = UIPanelRegistry(conn)

    if retire_prior_versions:
        # Retire any rows owned by an OLDER version of this descriptor.
        # We leave the same-version rows alone so re-running the
        # function is idempotent at the SQL level.
        await conn.execute(
            """
            UPDATE ui_panel_registrations
               SET retired    = TRUE,
                   retired_at = NOW()
             WHERE descriptor_id = $1
               AND descriptor_version <> $2
               AND retired = FALSE
            """,
            descriptor_id,
            descriptor_version,
        )

    persisted: list[PanelRegistration] = []
    for reg in parsed:
        persisted.append(await registry.register(reg))
    return persisted


__all__ = [
    "KIND_NAME",
    "LayoutSlotConflict",
    "PanelRegistration",
    "UIPanelDescriptorError",
    "UIPanelRegistry",
    "parse_descriptor_panels",
    "register_from_descriptor",
]
