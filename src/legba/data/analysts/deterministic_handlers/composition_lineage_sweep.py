# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``composition_lineage_sweep`` sub-handler — the system's lineage-integrity
verification over the COMPOSITION roots (P3-T6).

A NEW deterministic sub-handler (NOT an extension of ``integrity_sweep`` — that
is a single global count-based audit marked ``TRACE_ONLY``; a per-root multi-hop
BFS would change its contract, bloat its one run, and couple two cadences). This
one walks the ``derived_from`` graph BACKWARD from each recent composition root
(world_assessor / country_composition) via
:func:`legba.data.provenance.verify.validate_lineage` and reports whether the
tower's provenance holds at EVERY floor:

    world-read  →  country-read  →  unit sub-claim  →  signal (→ source)

``validate_lineage`` is SINGLE-TABLE (it walks ``analyst_outputs.derived_from``),
so a cross-table LEAF — the signal a unit sub-claim rests on lives in ``signals``,
not ``analyst_outputs`` — surfaces on its ``dangling`` list even on a HEALTHY
tower. So we POST-FILTER each root's ``dangling`` against the full lineage
CATALOG (the 7-table superset the P1-T8 reachable-click-path probe resolves
against): a ref that resolves to a real row in ANY catalog table is a VALID leaf
(dropped); only a ref that resolves to NOTHING is a TRUE break. Result:

  * a healthy tower reports 0 cycles / 0 (true) dangling / 0 depth_exhausted;
  * deleting (or orphaning the ``derived_from`` of) a unit sub-claim under a live
    country read makes that country read's ref resolve to nothing → the root is
    FLAGGED in the NAMED ``with_dangling`` sample.

Crucially — like ``integrity_sweep`` — it **refuses loud**: it requires a live
``deps.pg_pool`` and a missing relation propagates rather than being swallowed
into a zeroed clean finding. A 0-issue finding therefore means the BFS genuinely
ran clean across the swept roots, never that the sweep aborted.

Read-only AUDIT: it COUNTS + NAMES; it does NO repair (the prune stays an
operator-gated migration).

Target-agnostic META analyst: the descriptor declares no ``targets`` selector,
so the cadence heartbeat is a SINGLE global sweep. Registered via
``scripts/bringup_register_composition_lineage_sweep.py``.
"""
from __future__ import annotations

import logging
from typing import Any, Mapping
from uuid import UUID

from ...provenance.models import FindingPayload
from ...provenance.verify import validate_lineage
from ....runtime.analyst_method import AnalystMethodResult

logger = logging.getLogger(__name__)

SUB_HANDLER_NAME = "composition_lineage_sweep"

# The composition roots this sweep grades — the system's two composition
# analysts. Both write ``kind='finding'`` rows marked ``data.data.meta=true``.
_COMPOSITION_ANALYSTS: tuple[str, ...] = ("world_assessor", "country_composition")

# The derived_from BFS max depth (world → country → unit → signal → source is
# ~5 floors; 20 leaves generous headroom + is validate_lineage's own default).
_MAX_DEPTH = 20

# How many recent composition roots to sweep per run (bounded — the BFS is one
# fetchrow per node; a bounded root set keeps the run cheap + idempotent).
_ROOT_CAP = 200

# Default look-back window (hours) for "recent" composition roots. Overridable
# via ``options['window_hours']``.
_DEFAULT_WINDOW_HOURS = 48

# Capped NAMED sample of offending roots (mirrors integrity_sweep's
# _DANGLING_SAMPLE_CAP=25 count+sample contract).
_OFFENDER_CAP = 25

# Per-offender caps so a pathological root can't blow the finding body.
_CYCLES_PER_OFFENDER = 5
_DANGLING_PER_OFFENDER = 10


_ROOTS_SQL = """
SELECT ao.id, ao.analyst_id, ao.produced_at
FROM analyst_outputs ao
WHERE ao.kind = 'finding'
  AND ao.analyst_id = ANY($1::text[])
  AND (ao.data -> 'data' ->> 'meta') = 'true'
  AND ao.produced_at > NOW() - make_interval(hours => $2)
ORDER BY ao.produced_at DESC
LIMIT $3
"""

# Which of a root's SINGLE-TABLE ``dangling`` refs actually resolve to a real row
# somewhere in the lineage CATALOG (the 7-table superset the P1-T8 reachable
# probe resolves against). A ref present here is a VALID cross-table leaf
# (signal / fact / situation / hypothesis / entity / nexus) — NOT a break. A
# missing relation RAISES (refuse loud), same contract as integrity_sweep.
_CATALOG_RESOLVE_SQL = """
SELECT df.ref
FROM unnest($1::uuid[]) AS df(ref)
WHERE EXISTS (SELECT 1 FROM signals s          WHERE s.id  = df.ref)
   OR EXISTS (SELECT 1 FROM analyst_outputs ao WHERE ao.id = df.ref)
   OR EXISTS (SELECT 1 FROM facts f            WHERE f.id  = df.ref)
   OR EXISTS (SELECT 1 FROM situations si      WHERE si.id = df.ref)
   OR EXISTS (SELECT 1 FROM hypotheses h       WHERE h.id  = df.ref)
   OR EXISTS (SELECT 1 FROM entity_profiles ep WHERE ep.id = df.ref)
   OR EXISTS (SELECT 1 FROM nexuses nx         WHERE nx.id = df.ref)
"""


def _resolve_window_hours(options: Mapping[str, Any]) -> int:
    """Look-back window (hours) for 'recent' roots — options override + default."""
    raw = options.get("window_hours")
    try:
        hours = int(raw) if raw is not None else _DEFAULT_WINDOW_HOURS
    except (TypeError, ValueError):
        hours = _DEFAULT_WINDOW_HOURS
    return hours if hours > 0 else _DEFAULT_WINDOW_HOURS


async def _resolve_catalog(conn: Any, ids: list[UUID]) -> set[UUID]:
    """The subset of ``ids`` present in ANY lineage-catalog table (valid leaves).

    A missing relation RAISES (not caught) — refuse loud. An empty ``ids`` short-
    circuits to an empty set (no query)."""
    if not ids:
        return set()
    rows = await conn.fetch(_CATALOG_RESOLVE_SQL, list(ids))
    return {r["ref"] for r in rows}


def _build_finding(
    *,
    swept: int,
    ok: int,
    with_cycles: int,
    with_dangling: int,
    depth_exhausted: int,
    window_hours: int,
    offenders: list[dict[str, Any]],
) -> FindingPayload:
    issues = with_cycles + with_dangling + depth_exhausted
    clean = issues == 0
    title = (
        f"Composition lineage sweep: {ok}/{swept} roots clean"
        if swept
        else "Composition lineage sweep: no composition roots in window"
    )
    body_lines = [
        f"swept={swept}",
        f"ok={ok}",
        f"with_cycles={with_cycles}",
        f"with_dangling={with_dangling}",
        f"depth_exhausted={depth_exhausted}",
        f"window_hours={window_hours}",
    ]
    if offenders:
        body_lines.append(f"offending_roots ({len(offenders)}, cap={_OFFENDER_CAP}):")
        for o in offenders:
            body_lines.append(
                f"  root={o['root_id']} analyst={o['analyst_id']} "
                f"cycles={len(o['cycles'])} dangling={len(o['dangling'])} "
                f"depth_exhausted={o['depth_exhausted']}"
            )
            for d in o["dangling"]:
                body_lines.append(f"    - dangling_ref={d}")
            for c in o["cycles"]:
                body_lines.append(f"    - cycle={' -> '.join(c)}")
    tags = ["deterministic", SUB_HANDLER_NAME]
    tags.append("composition_lineage_clean" if clean else "composition_lineage_issues")
    return FindingPayload(
        title=title[:2048],
        body="\n".join(body_lines)[:65536],
        confidence=1.0,
        evidence=[],
        tags=tags,
        data={
            "sub_handler": SUB_HANDLER_NAME,
            "swept": swept,
            "ok": ok,
            "with_cycles": with_cycles,
            "with_dangling": with_dangling,
            "depth_exhausted": depth_exhausted,
            "window_hours": window_hours,
            "offenders": offenders,
            "offenders_cap": _OFFENDER_CAP,
        },
    )


async def handle(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: Any | None,
) -> AnalystMethodResult:
    """Sub-handler entry point — see module docstring.

    REFUSES LOUD: requires a live ``deps.pg_pool``; a missing relation (roots
    query or catalog resolve) propagates rather than being swallowed into a
    zeroed clean finding. Emits ONE honest summary finding — a 0-issue finding
    means the multi-floor BFS genuinely ran clean across the swept roots.
    """
    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    if pool is None:
        raise RuntimeError(
            "composition_lineage_sweep requires a live deps.pg_pool — refusing "
            "to emit a zeroed clean lineage finding without walking the tower"
        )

    window_hours = _resolve_window_hours(options)
    swept = 0
    ok = 0
    with_cycles = 0
    with_dangling = 0
    depth_exhausted = 0
    offenders: list[dict[str, Any]] = []

    async with pool.acquire() as conn:
        roots = await conn.fetch(
            _ROOTS_SQL, list(_COMPOSITION_ANALYSTS), window_hours, _ROOT_CAP
        )
        for r in roots:
            root_id = r["id"]
            report = await validate_lineage(
                conn, "analyst_outputs", root_id, max_depth=_MAX_DEPTH
            )
            # Post-filter single-table dangling against the full catalog — a
            # cross-table LEAF (signal/fact/…) is valid, only an unresolvable ref
            # is a TRUE break.
            true_dangling: list[UUID] = []
            if report.dangling:
                resolvable = await _resolve_catalog(conn, list(report.dangling))
                true_dangling = [d for d in report.dangling if d not in resolvable]

            swept += 1
            has_cycle = bool(report.cycles)
            has_dangling = bool(true_dangling)
            has_depth = bool(report.depth_exhausted)
            if not (has_cycle or has_dangling or has_depth):
                ok += 1
                continue
            if has_cycle:
                with_cycles += 1
            if has_dangling:
                with_dangling += 1
            if has_depth:
                depth_exhausted += 1
            if len(offenders) < _OFFENDER_CAP:
                offenders.append(
                    {
                        "root_id": str(root_id),
                        "analyst_id": str(r["analyst_id"]),
                        "cycles": [
                            [str(x) for x in c]
                            for c in report.cycles[:_CYCLES_PER_OFFENDER]
                        ],
                        "dangling": [
                            str(x) for x in true_dangling[:_DANGLING_PER_OFFENDER]
                        ],
                        "depth_exhausted": has_depth,
                    }
                )

    issues = with_cycles + with_dangling + depth_exhausted
    if issues:
        logger.warning(
            "composition_lineage_sweep.issues swept=%d ok=%d cycles=%d "
            "dangling=%d depth_exhausted=%d",
            swept, ok, with_cycles, with_dangling, depth_exhausted,
        )
    else:
        logger.info("composition_lineage_sweep.clean swept=%d", swept)

    finding = _build_finding(
        swept=swept,
        ok=ok,
        with_cycles=with_cycles,
        with_dangling=with_dangling,
        depth_exhausted=depth_exhausted,
        window_hours=window_hours,
        offenders=offenders,
    )
    return AnalystMethodResult(
        finding=finding,
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
    )


__all__ = ["handle"]
