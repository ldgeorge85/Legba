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

THE FRAME GAUGE RIDES HERE TOO (TITLE-FRAME-FIX, 2026-09-01). This sweep already
runs on a global cadence over exactly the composition roots, on a schedule
offset to land after the world read — which is the same shape a headline-grammar
metric needs, so ``VOICE_ORGANIC_REVIEW_2026-09-01`` §5.2 puts it here rather
than building a second analyst: *"Extend, don't build."* The gauge is
:mod:`._title_frame_gauge`, it is pure text over ``title`` / ``body``, and it
obeys this module's contract exactly — it COUNTS + NAMES and changes nothing.

It deliberately does NOT gate. No new tag, no new alert kind, no effect on the
``composition_lineage_clean`` / ``_issues`` call: those belong to LINEAGE, and a
frame rate is not a lineage break. The numbers land in the finding's ``data``
and body so a floor can be set later against observed values rather than
guessed ones.

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
from ._title_frame_gauge import gauge_rows

logger = logging.getLogger(__name__)

SUB_HANDLER_NAME = "composition_lineage_sweep"

# The composition roots this sweep grades — the system's two composition
# analysts. Both write ``kind='finding'`` rows marked ``data.data.meta=true``.
#
# D-6 (2026-09-04) adds the ASSESSMENT CHANNEL. Its lineage is one edge —
# ``derived_from = [assembly_id]``, exactly one element, which IS its input
# restriction — so the BFS from it costs one extra hop before it lands on a root
# this sweep already walks. That single edge is the thing most worth validating:
# a dangling one would mean the channel published an argument about a record
# nobody can open.
_COMPOSITION_ANALYSTS: tuple[str, ...] = (
    "world_assessor",
    "country_composition",
    "world_assessment",
    # P3 LANE A. Same one-edge lineage as the world channel —
    # ``derived_from = [country_composition head id]``, exactly one element —
    # and the same reason to walk it: a dangling edge here means a country board
    # was published an argument about a record nobody can open.
    "country_assessment",
)

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

# ---------------------------------------------------------------------------
# FRAME GAUGE knobs (TITLE-FRAME-FIX, 2026-09-01)
# ---------------------------------------------------------------------------

# The gauge reads ALL FOUR composition tiers, not just the two whose lineage
# this sweep walks. The BFS is expensive per root and is rightly scoped to the
# two roots that own the tower's provenance; the gauge is one pass of regex over
# text a single query already returned, so scoping it narrower would cost
# nothing and buy nothing. It also matters that region and thematic are IN: the
# review's §2.b cascade is world <- region <- country and the thematic tier's
# own prompt states the propagation as a design goal, so a gauge blind to the
# middle floors could not tell a fixed headline from a headline whose input was
# already crowned one floor down.
#
# D-6: the ASSESSMENT CHANNEL joins the gauge, and post-cutover it is the ONLY
# tier the gauge can still say anything about. Under the assembly a composition's
# title is DETERMINISTIC (masthead + the lead span's own quoted fragment, or a
# shape line, or the masthead and two counts), so its frame rate stops being a
# measurement of a model and becomes a measurement of a format string. The
# Assessment's title is the model's, under the 2026-09-01 contract unchanged —
# so §5.3 G6's "no 5-day run shares a title frame" moves here with the voice.
_GAUGE_ANALYSTS: tuple[str, ...] = (
    "world_assessor",
    "region_composition",
    "country_composition",
    "escalation_composition",
    "world_assessment",
    # P3 LANE A. Post-demotion the gauge can only say something about a tier
    # that still AUTHORS a title, and after D-5 there are exactly two of those.
    # This is the second.
    "country_assessment",
)

# Trailing window for the gauge, in DAYS rather than the lineage sweep's hours.
# 14 is review §5.2's own window for dimension coverage, and it is the shortest
# window on which a crown streak is meaningful: the world tier runs twice a day,
# so 14 days is 28 runs against a longest-observed streak of 10.
_GAUGE_WINDOW_DAYS = 14

# Row cap for the gauge query. 14 days x ~2 world + ~10 region + ~65 country
# runs a day lands near 1,100, so the cap binds on the country tier and the
# metrics for it are explicitly over the most recent N. Bounded on purpose: this
# handler must stay cheap enough to keep sharing a cadence with the BFS.
_GAUGE_ROW_CAP = 1200


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

# The FRAME GAUGE's own row set. Separate from ``_ROOTS_SQL`` on purpose: that
# query's 48h window and 200-root cap are sized for a per-root BFS, while the
# gauge needs a longer, ORDERED run of titles (a crown streak is a statement
# about consecutive runs) and needs the region and thematic tiers the BFS does
# not walk. ``produced_at DESC`` is the run order the streak metric consumes.
#
# D-5 — THE ROLLUP TIER IS EXCLUDED, and this one is load-bearing rather than
# tidy. The frame gauge measures whether a MODEL keeps reaching for the same
# title frame run after run (§5.3 G6: "no 5-day run shares a title frame"). A
# ``region_rollup.v1`` title is DETERMINISTIC — "<Region>, <date> — N of M
# member country reads carried" — so it shares its frame with every other
# rollup by construction, forever, on purpose. Left in, it would report a
# permanent 100% frame lock on the region tier and drag the crowned-rate the
# gauge exists to watch, i.e. a real alarm about a real fact that means nothing.
# The gauge's question is not askable of a row nobody wrote.
#
# The predicate is ``IS DISTINCT FROM`` because the JSONB path is NULL on every
# pre-D-2 row and ``NULL <> 'rollup'`` is NULL, not TRUE — a plain ``<>`` would
# silently empty the gauge across the whole historical corpus.
_GAUGE_SQL = """
SELECT ao.analyst_id, ao.title, ao.body, ao.produced_at
FROM analyst_outputs ao
WHERE ao.kind = 'finding'
  AND ao.analyst_id = ANY($1::text[])
  AND (ao.data -> 'data' ->> 'meta') = 'true'
  AND (ao.data -> 'data' -> 'assembly' ->> 'regime') IS DISTINCT FROM 'rollup'
  AND ao.title IS NOT NULL
  AND ao.produced_at > NOW() - make_interval(days => $2)
ORDER BY ao.produced_at DESC
LIMIT $3
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


def _pct(value: Any) -> str:
    """A ratio as a percentage, or ``n/a`` when the denominator was empty.

    ``None`` is a real answer here, not a missing one — see ``gauge_rows``: a
    single read has no consecutive pair, so its churn is undefined and printing
    ``0.0%`` would read as "perfectly stable" when nothing was measured.
    """
    return "n/a" if value is None else f"{float(value) * 100:.1f}%"


def _num(value: Any) -> str:
    return "n/a" if value is None else f"{float(value):.3f}"


def _build_finding(
    *,
    swept: int,
    ok: int,
    with_cycles: int,
    with_dangling: int,
    depth_exhausted: int,
    window_hours: int,
    offenders: list[dict[str, Any]],
    frame_gauge: dict[str, dict[str, Any]],
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
    if frame_gauge:
        body_lines.append(f"frame_gauge (trailing {_GAUGE_WINDOW_DAYS}d):")
        for analyst, m in frame_gauge.items():
            body_lines.append(
                f"  {analyst} n={m['n']} frame={_pct(m['frame_rate'])} "
                f"core={_pct(m['frame_rate_core'])} "
                f"roll_call={_pct(m['roll_call_rate'])} "
                f"streak_max={m['crown_streak_max']} "
                f"churn={_pct(m['crown_churn'])} "
                f"dims={m['dimension_coverage']}/{m['dimensions_total']} "
                f"concordance={_num(m['concordance'])}"
            )
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
            "frame_gauge": frame_gauge,
            "frame_gauge_window_days": _GAUGE_WINDOW_DAYS,
            "frame_gauge_row_cap": _GAUGE_ROW_CAP,
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
    frame_gauge: dict[str, dict[str, Any]] = {}
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

        # THE FRAME GAUGE. Same connection, same refuse-loud contract — a
        # missing relation propagates rather than being swallowed into a
        # zeroed-out gauge, because a gauge that reports "frame rate 0.0%"
        # after failing to read any titles is worse than no gauge at all.
        gauge_source = await conn.fetch(
            _GAUGE_SQL, list(_GAUGE_ANALYSTS), _GAUGE_WINDOW_DAYS, _GAUGE_ROW_CAP
        )
        frame_gauge = gauge_rows(gauge_source)

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
        frame_gauge=frame_gauge,
    )
    return AnalystMethodResult(
        finding=finding,
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
    )


__all__ = ["handle"]
