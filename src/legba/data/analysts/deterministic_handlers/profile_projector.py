# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W9 VIEW-1 — the profile projector: whole rebuild of each profile's plane.

Every plane in the process's catalog whose profile declares a view
(``legba.data.profile_projection.plane_catalog``) is rebuilt from scratch on
each run: the view's static sections (resolved from the manifest at export
time — tiers, labels, bands, scope, the units' roster half) plus what this
handler reads off the substrate:

  * ``subject_groups`` — the ``frame`` family (FRAME-1,
    ``legba.data.frames``): every frame in the subtree of each declared kind,
    with its label, parent, member rule and its resolved member target heads;
  * ``units`` — each roster unit's OWN cadence from its live descriptor head
    (``cadence.fallback_schedule`` and the interval it implies); a unit with
    no head keeps a null cadence and is named in the receipt's ``unmeasured``;
  * ``crosswalk`` — the ``crosswalk`` family's rows under the profile's
    namespace (DEC-12).

The write is ONE transaction per plane::

    DELETE FROM profile_projection WHERE plane = $1;
    INSERT ... SELECT FROM unnest(...);           -- every row, now()
    INSERT ... the (plane, '_meta', 'build') receipt row

so a reader sees the old plane until the commit and the new one after, and a
rebuild of one plane never touches another (the plane-isolation rule). There
is NO incremental path — the graph_projector rule: a structure only ever
built by SELECT can be stale, never wrong.

**Determinism.** Every row's key, order and value is a pure function of the
view and the substrate snapshot (no clock, no sampling); only
``projected_at`` and the receipt's ``build_seconds`` differ between two builds
of the same snapshot.

**The gate.** ``LEGBA_PROFILE_PROJECTION`` (default OFF): off, the handler
returns an honest no-op receipt and the table is never touched.
``options.dry_run`` builds every plane's rows and reports the counts without
writing.

Descriptor: ``descriptors/analyst_profile_projector.yaml`` (state: draft).
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Mapping, Sequence

from ... import frames
from ...profile_projection import (
    META_KEY,
    META_SECTION,
    SECTIONS,
    PlaneCatalog,
    crosswalk_pair,
    plane_catalog,
    profile_projection_enabled,
)
from ...provenance.models import FindingPayload
from ...registry.source_freshness import cadence_interval_minutes
from ...schemas.vocabulary import CROSSWALK_FAMILY
from ....runtime.analyst_method import AnalystMethodResult

logger = logging.getLogger(__name__)

SUB_HANDLER_NAME = "profile_projector"

#: Deploy marker — the roll checklist's grep target. Bump on a shape change.
PROFILE_PROJECTION_VERSION = "2026-10/view1"

#: Each roster unit's own declared cadence, off its live head (the same read
#: the absence route makes for its measuring analysts).
_UNIT_CADENCE_SQL = """
SELECT descriptor_id, body->'cadence'->>'fallback_schedule' AS schedule
  FROM public.analyst_descriptors
 WHERE is_head
   AND descriptor_id = ANY($1::text[])
"""

#: The crosswalk rows under one profile namespace (`<ns>.<axis>:<value>`).
#: ``starts_with`` rather than LIKE: a namespace's underscores are LIKE
#: wildcards.
_CROSSWALK_SQL = """
SELECT value, parent, notes
  FROM public.vocabulary_entries
 WHERE family = $1
   AND deprecated IS NULL
   AND starts_with(value, $2)
 ORDER BY value
"""

_DELETE_PLANE_SQL = "DELETE FROM public.profile_projection WHERE plane = $1"

#: Every row of one plane in ONE statement; ``now()`` is the transaction's,
#: so the whole build shares one ``projected_at``.
_INSERT_ROWS_SQL = """
INSERT INTO public.profile_projection (plane, section, key, ord, value, projected_at)
SELECT $1, u.section, u.key, u.ord, u.value::jsonb, now()
  FROM unnest($2::text[], $3::text[], $4::int[], $5::text[]) AS u(section, key, ord, value)
"""


def _finding(data: Mapping[str, Any], *, changed: bool) -> AnalystMethodResult:
    """The projector's per-run receipt (never lands on a trust surface)."""
    return AnalystMethodResult(
        finding=FindingPayload(
            title=f"Profile projector ({'rebuilt' if changed else 'no-op'})",
            body=f"profile_projector {PROFILE_PROJECTION_VERSION}: {dict(data)}",
            confidence=1.0,
            evidence=[],
            tags=["deterministic", SUB_HANDLER_NAME, "view1_projection"],
            data={
                "sub_handler": SUB_HANDLER_NAME,
                "pipeline_version": PROFILE_PROJECTION_VERSION,
                **dict(data),
            },
        ),
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
        derived_from=[],
        force_trace_only=not changed,
    )


def _coerce_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# The substrate-derived sections
# ---------------------------------------------------------------------------


async def _subject_groups(
    conn: Any, kinds: Sequence[str],
) -> tuple[list[dict[str, Any]], str | None]:
    """Every frame in the subtree of each kind, kinds in declared order, the
    kind first and its descendants by id. ``(rows, unmeasured_reason)``."""
    catalog = await frames.load_frame_catalog(conn)
    if not catalog.loaded:
        detail = f": {catalog.detail}" if catalog.detail else ""
        return [], f"the {frames.FRAME_FAMILY} family is {catalog.status}{detail}"
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    missing: list[str] = []
    for kind in kinds:
        subtree = catalog.subtree(kind)
        if not subtree:
            missing.append(kind)
            continue
        for f in subtree:
            if f.id in seen:
                continue
            seen.add(f.id)
            members = await frames.resolve_members(f.id, conn, catalog=catalog)
            rows.append({"key": f.id, "value": {
                "kind": kind,
                "label": f.label,
                "parent": f.parent_id,
                "member_rule": list(f.members),
                "members": members,
                "member_analysts": list(f.member_analysts),
            }})
    reason = (
        f"kind(s) not registered in the {frames.FRAME_FAMILY} family: {', '.join(missing)}"
        if missing else None
    )
    return rows, reason


async def _unit_cadences(
    conn: Any, static_rows: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], str | None]:
    """The roster rows, each with its head's schedule and the interval it
    implies; null (never a guessed default) when the head or cadence is
    missing."""
    ids = [str(r["key"]) for r in static_rows]
    heads = {
        r["descriptor_id"]: r["schedule"]
        for r in await conn.fetch(_UNIT_CADENCE_SQL, ids)
    } if ids else {}
    rows: list[dict[str, Any]] = []
    headless: list[str] = []
    for r in static_rows:
        unit = str(r["key"])
        schedule = heads.get(unit)
        if unit not in heads:
            headless.append(unit)
        minutes = cadence_interval_minutes(schedule) if schedule else None
        rows.append({"key": unit, "value": {
            **dict(r["value"]),
            "schedule": schedule,
            "cadence_hours": round(minutes / 60.0, 4) if minutes else None,
        }})
    reason = f"no descriptor head for unit(s): {', '.join(headless)}" if headless else None
    return rows, reason


async def _crosswalk(conn: Any, namespace: str) -> tuple[list[dict[str, Any]], int]:
    """The namespace's crosswalk rows, by value. ``(rows, refused)`` — a row
    that is not a well-formed pair is skipped and counted, never guessed."""
    rows: list[dict[str, Any]] = []
    refused = 0
    for r in await conn.fetch(_CROSSWALK_SQL, CROSSWALK_FAMILY, f"{namespace}."):
        pair = crosswalk_pair(r["value"], r["parent"], r["notes"])
        if pair is None or pair.from_family.split(".", 1)[0] != namespace:
            refused += 1
            continue
        rows.append({"key": pair.key, "value": {
            "from_family": pair.from_family,
            "from_value": pair.from_value,
            "to_family": pair.to_family,
            "to_value": pair.to_value,
            "notes": pair.notes,
        }})
    return rows, refused


# ---------------------------------------------------------------------------
# One plane
# ---------------------------------------------------------------------------


async def plane_rows(
    conn: Any, entry: Mapping[str, Any],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str], int]:
    """``(sections, unmeasured, crosswalk_refused)`` for one plane entry —
    read-only, deterministic."""
    view = entry["view"]
    static = view.get("sections") or {}
    derive = view.get("derive") or {}
    sections: dict[str, list[dict[str, Any]]] = {
        s: [dict(r) for r in static[s]] for s in SECTIONS if s in static
    }
    unmeasured: dict[str, str] = {}
    refused = 0
    if "subject_groups" in derive:
        rows, why = await _subject_groups(conn, derive["subject_groups"]["kinds"])
        sections["subject_groups"] = rows
        if why:
            unmeasured["subject_groups"] = why
    if "units" in derive and "units" in sections:
        rows, why = await _unit_cadences(conn, sections["units"])
        sections["units"] = rows
        if why:
            unmeasured["units"] = why
    if "crosswalk" in derive:
        rows, refused = await _crosswalk(conn, derive["crosswalk"]["namespace"])
        sections["crosswalk"] = rows
        if refused:
            unmeasured["crosswalk"] = f"{refused} malformed crosswalk row(s) skipped"
    return sections, unmeasured, refused


async def build_plane(
    conn: Any, entry: Mapping[str, Any], *, dry_run: bool = False,
) -> dict[str, Any]:
    """Rebuild ONE plane (one transaction); the receipt as a dict."""
    t0 = time.monotonic()
    plane = str(entry["plane"])
    sections, unmeasured, _refused = await plane_rows(conn, entry)
    s_names: list[str] = []
    keys: list[str] = []
    ords: list[int] = []
    values: list[str] = []
    for section in SECTIONS:
        for i, row in enumerate(sections.get(section, ())):
            s_names.append(section)
            keys.append(str(row["key"]))
            ords.append(i)
            values.append(json.dumps(row["value"], sort_keys=True, ensure_ascii=True))
    section_counts = {s: len(sections[s]) for s in SECTIONS if s in sections}
    receipt: dict[str, Any] = {
        "version": PROFILE_PROJECTION_VERSION,
        "profile": entry.get("profile"),
        "view_digest": entry["view"].get("digest"),
        "row_count": len(keys),
        "section_counts": section_counts,
        "unmeasured": unmeasured,
    }
    if not dry_run:
        async with conn.transaction():
            await conn.execute(_DELETE_PLANE_SQL, plane)
            if keys:
                await conn.execute(_INSERT_ROWS_SQL, plane, s_names, keys, ords, values)
            receipt["build_seconds"] = round(time.monotonic() - t0, 3)
            await conn.execute(
                _INSERT_ROWS_SQL, plane, [META_SECTION], [META_KEY], [0],
                [json.dumps(receipt, sort_keys=True, ensure_ascii=True)],
            )
    else:
        receipt["build_seconds"] = round(time.monotonic() - t0, 3)
    return {"plane": plane, "dry_run": dry_run, **receipt}


def _built_planes(catalog: PlaneCatalog) -> list[Mapping[str, Any]]:
    return [catalog.planes[p] for p in sorted(catalog.planes) if catalog.planes[p].get("view")]


async def handle(
    inputs: Sequence[Mapping[str, Any]] | None = None,
    options: Mapping[str, Any] | None = None,
    deps: Any = None,
    **_kwargs: Any,
) -> AnalystMethodResult:
    """Rebuild every declared plane. Flag-gated on ``LEGBA_PROFILE_PROJECTION``."""
    options = options or {}
    if not profile_projection_enabled():
        return _finding({"projection_enabled": False, "planes": []}, changed=False)
    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    if pool is None:
        return _finding({"synthetic": True, "planes": []}, changed=False)
    dry_run = _coerce_bool(options.get("dry_run"), False)
    catalog = plane_catalog()
    receipts: list[dict[str, Any]] = []
    async with pool.acquire() as conn:
        for entry in _built_planes(catalog):
            receipts.append(await build_plane(conn, entry, dry_run=dry_run))
    for r in receipts:
        logger.info(
            "profile_projector.build plane=%s rows=%d seconds=%.3f dry_run=%s unmeasured=%s",
            r["plane"], r["row_count"], r["build_seconds"], dry_run, sorted(r["unmeasured"]),
        )
    return _finding(
        {
            "projection_enabled": True,
            "dry_run": dry_run,
            "catalog": catalog.origin,
            "planes": receipts,
            "planes_without_view": sorted(
                p for p, e in catalog.planes.items() if not e.get("view")
            ),
        },
        changed=not dry_run and bool(receipts),
    )
