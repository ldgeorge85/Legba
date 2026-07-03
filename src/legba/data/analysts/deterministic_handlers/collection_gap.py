# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``collection_gap`` sub-handler — S3-T3 collection-requirements analyst.

A DETERMINISTIC (no-LLM) META analyst on a MONTHLY cadence over the banded
scorecard rows (``kind='scorecard'``; produced by
:mod:`scorecard_producer` — one persisted per-country card whose
``data->'data'->'bands'->'dimensions'`` carries the T1
:func:`scorecard_banding.band_target` verdict). It reads the honest
``insufficient-evidence`` signal the banding engine already computes and turns
it into a forward-looking COLLECTION requirement: which ``desk × dimension``
cells are STARVED, WHY (the aggregated banding ``reason``), and which SOURCE
CLASSES would plausibly feed them.

What "starved" means
--------------------

A cell = one ``(desk, dimension)`` where ``desk`` is the scorecard's
``target_id`` (a country desk) and ``dimension`` is one of the fixed bounded-unit
:data:`scorecard_banding.DIMENSIONS`. A cell is STARVED when it is
``band == 'insufficient-evidence'`` in that desk's CURRENT (latest-in-window)
scorecard — a live gap, not a settled-history one. Over the window (all scorecard
rows for the desk, HEAD + superseded, in the lookback) we ALSO aggregate the
distinct insufficient reasons + how PERSISTENT the starvation is (a cell
insufficient in every card all month is a harder collection gap than a
one-off) — this is the ``insufficient-evidence`` reason aggregation the task asks
for, keyed ``desk × dimension``.

Ranking (why US tops the list)
------------------------------

Cells are ordered so the most-starved DESKS surface first: primary key =
``desk_starved_dims`` DESC (a desk with ALL six dimensions insufficient — an
all-insufficient card — sorts every one of its cells above a desk starved in a
single dimension), then ``insufficient_count`` DESC (persistence), then the desk
id, then the fixed dimension order. So an "US all-insufficient" desk tops the
gap list.

Source classes
--------------

:data:`SOURCE_CLASSES_BY_DIMENSION` is the static collection-doctrine map from a
dimension to the ``source_class`` vocabulary (``reporting`` / ``analysis`` /
``official`` / ``state_media``) that would plausibly feed it. It is a fixed,
auditable table — NEVER model-generated — so a collection manager reading a
starved ``narrative_coordination`` cell sees ``state_media`` named as the
plausible feed, an ``energy_security`` cell sees ``official`` first, etc.

Honesty / idempotency (the findings-feed dedup lesson)
------------------------------------------------------

The "collection requirements" summary is a per-run RECEIPT. It reaches the feed
ONLY when at least one cell is starved:

  * NO starved cell → ``force_trace_only=True`` (trace + no feed row) so an
    idempotent monthly re-run over a fully-fed roster never repeats a "nothing
    starved" row.
  * Gaps found, but the gap set is BYTE-IDENTICAL to the last EMITTED
    collection_gap finding (the same scorecards re-swept) → likewise
    ``force_trace_only=True`` (live path only; degrade-to-EMIT on any dedup
    error). The body is deterministic from the gap set, so a body match == an
    unchanged gap set — the same dedup contract :mod:`indicator_tracker` uses.

``deps=None`` runs the synthetic (no-DB) path: ``inputs`` are pre-shaped
scorecard rows (``target_id`` / ``id`` / ``produced_at`` + either a direct
``dimensions`` map or the persisted ``data`` shape) — used by the unit tests.
"""

from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from typing import Any, Mapping

from ...provenance.models import FindingPayload
from ....runtime.analyst_method import AnalystMethodResult
from . import scorecard_banding

logger = logging.getLogger(__name__)

SUB_HANDLER_NAME = "collection_gap"

#: The fixed dimensions = the bounded-unit analyst_ids the scorecard bands, in
#: their canonical order (imported so a new unit added to the tower flows through
#: with zero drift here).
DIMENSIONS: tuple[str, ...] = scorecard_banding.DIMENSIONS

#: The banding sentinel for a dimension with no qualifying verified claim.
INSUFFICIENT: str = scorecard_banding.INSUFFICIENT

#: Static collection-doctrine map: dimension → the ``source_class`` vocabulary
#: (S1-T8: reporting / analysis / official / state_media) that would plausibly
#: FEED that dimension, ordered by collection priority. A fixed, auditable table
#: — never model-generated. Keep the keys in sync with :data:`DIMENSIONS`.
SOURCE_CLASSES_BY_DIMENSION: dict[str, tuple[str, ...]] = {
    # Succession / cabinet / gazette signal — official announcements first,
    # wire reporting, then think-tank succession analysis.
    "leadership_transition": ("official", "reporting", "analysis"),
    # Energy statistics / ministry / regulator data first, wire reporting,
    # market analysis.
    "energy_security": ("official", "reporting", "analysis"),
    # Conflict / mobilization signal — wire reporting + official MOD/MFA
    # statements, conflict-event analysis, and adversary state media as framing.
    "escalation": ("reporting", "official", "analysis", "state_media"),
    # Influence / framing — state media is the PRIMARY evidence class here,
    # then reporting + narrative analysis.
    "narrative_coordination": ("state_media", "reporting", "analysis"),
    # Unrest / protest / cohesion — wire reporting + protest-event analysis +
    # official statistics.
    "internal_stability": ("reporting", "analysis", "official"),
    # Force posture / procurement — official MOD data + defense analysis
    # (SIPRI-class) + wire reporting.
    "military_posture": ("official", "analysis", "reporting"),
}

#: Fallback classes for any dimension not in the doctrine map (defensive — every
#: :data:`DIMENSIONS` entry is mapped above).
_DEFAULT_SOURCE_CLASSES: tuple[str, ...] = ("reporting", "analysis", "official")

#: Only scorecards produced within this many days are aggregated — a monthly
#: cadence with a >month window so a run always sees the full prior month of
#: cards (HEAD + superseded). Override via ``options['window_days']``.
_DEFAULT_WINDOW_DAYS: int = 35

#: Per-run safety cap on how many gap cells ride inside the finding data / body.
_MAX_GAPS_IN_FINDING: int = 500


# ---------------------------------------------------------------------------
# Extraction (shared by the live + synthetic paths)
# ---------------------------------------------------------------------------


def _parse_data(raw: Any) -> dict[str, Any]:
    """Normalize a scorecard row's ``data`` to a dict (tolerate a JSONB str)."""
    if raw is None:
        return {}
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


def _extract_dimensions(row: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """The per-dimension band verdicts carried by one scorecard row.

    Reads, in priority order:
      1. a direct ``row['dimensions']`` map (the convenience shape unit tests
         author);
      2. the persisted payload shape — ``analyst_outputs.data`` is the full
         ``ScorecardPayload`` model_dump, so the banded dimensions live at
         ``data->'data'->'bands'->'dimensions'`` (mirrors the v3 read route in
         ``registry.v3_api.eval_country_scorecard``); a top-level
         ``data->'bands'->'dimensions'`` is tolerated as a fallback.

    Returns only well-shaped ``{dimension: {band, reason, ...}}`` entries; a
    malformed value is dropped (degrade-not-drop).
    """
    direct = row.get("dimensions")
    if isinstance(direct, Mapping):
        return {str(k): dict(v) for k, v in direct.items() if isinstance(v, Mapping)}
    data = _parse_data(row.get("data"))
    inner = data.get("data")
    bands = inner.get("bands") if isinstance(inner, Mapping) else None
    if not isinstance(bands, Mapping):
        bands = data.get("bands")
    dims = bands.get("dimensions") if isinstance(bands, Mapping) else None
    if isinstance(dims, Mapping):
        return {str(k): dict(v) for k, v in dims.items() if isinstance(v, Mapping)}
    return {}


def _sort_key(row: Mapping[str, Any]) -> tuple[Any, str]:
    # Newest card first: latest produced_at, tie-broken by largest id.
    return (row.get("produced_at"), str(row.get("id")))


def _source_classes(dimension: str) -> list[str]:
    return list(SOURCE_CLASSES_BY_DIMENSION.get(dimension, _DEFAULT_SOURCE_CLASSES))


# ---------------------------------------------------------------------------
# Aggregation core (pure — testable without a DB)
# ---------------------------------------------------------------------------


def aggregate_gaps(
    rows: list[dict[str, Any]],
    *,
    dimensions: tuple[str, ...] = DIMENSIONS,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Aggregate insufficient-evidence over ``desk × dimension`` → the gap list.

    Groups the scorecard rows by desk (``target_id``); within each desk it
    picks the CURRENT (latest-in-window) card to decide which cells are STARVED,
    and aggregates over ALL the desk's window cards to count how many were
    insufficient + which reasons drove it. A cell is a gap iff it is
    ``band == INSUFFICIENT`` in the CURRENT card.

    Returns ``(gaps, stats)``. Each gap cell::

        {desk, dimension, reason, reasons, insufficient_count,
         window_scorecards, persistence, source_classes,
         latest_scorecard_id, desk_starved_dims}

    ordered so the most-starved desks surface first (``desk_starved_dims`` DESC,
    then ``insufficient_count`` DESC, then desk id, then dimension order) — an
    all-insufficient desk (e.g. US) tops the list.
    """
    by_desk: dict[str, list[dict[str, Any]]] = defaultdict(list)
    valid_rows = 0
    for r in rows:
        desk = r.get("target_id")
        if desk is None:
            continue
        by_desk[str(desk)].append(r)
        valid_rows += 1

    dim_order = {d: i for i, d in enumerate(dimensions)}
    gaps: list[dict[str, Any]] = []

    for desk, members in by_desk.items():
        ordered = sorted(members, key=_sort_key, reverse=True)
        latest = ordered[0]
        latest_dims = _extract_dimensions(latest)

        # Aggregate over the whole window for this desk.
        agg: dict[str, dict[str, Any]] = {
            dim: {"insufficient": 0, "total": 0, "reasons": Counter()}
            for dim in dimensions
        }
        for member in members:
            mdims = _extract_dimensions(member)
            for dim in dimensions:
                d = mdims.get(dim)
                if not isinstance(d, Mapping):
                    continue
                agg[dim]["total"] += 1
                if d.get("band") == INSUFFICIENT:
                    agg[dim]["insufficient"] += 1
                    reason = str(d.get("reason") or "unspecified")
                    agg[dim]["reasons"][reason] += 1

        # A gap = a dimension INSUFFICIENT in the CURRENT (latest) card.
        for dim in dimensions:
            ld = latest_dims.get(dim)
            if not isinstance(ld, Mapping) or ld.get("band") != INSUFFICIENT:
                continue
            a = agg[dim]
            total = int(a["total"])
            insufficient = int(a["insufficient"])
            gaps.append({
                "desk": desk,
                "dimension": dim,
                "reason": str(ld.get("reason") or "unspecified"),
                "reasons": dict(a["reasons"]),
                "insufficient_count": insufficient,
                "window_scorecards": total,
                "persistence": (
                    round(insufficient / total, 3) if total else None
                ),
                "source_classes": _source_classes(dim),
                "latest_scorecard_id": (
                    str(latest.get("id")) if latest.get("id") is not None else None
                ),
            })

    # Annotate each cell with its desk's total starved-dimension count (the
    # primary ranking key) — an all-insufficient desk lifts all its cells.
    starved_by_desk: Counter[str] = Counter(g["desk"] for g in gaps)
    for g in gaps:
        g["desk_starved_dims"] = int(starved_by_desk[g["desk"]])

    gaps.sort(
        key=lambda g: (
            -g["desk_starved_dims"],
            -g["insufficient_count"],
            g["desk"],
            dim_order.get(g["dimension"], len(dimensions)),
        )
    )

    # Per-dimension rollup for collection prioritization (how many desks are
    # starved in each dimension + the plausible feed classes).
    desks_by_dim: dict[str, set[str]] = defaultdict(set)
    for g in gaps:
        desks_by_dim[g["dimension"]].add(g["desk"])
    by_dimension = {
        dim: {
            "desks_starved": len(desks_by_dim.get(dim, set())),
            "source_classes": _source_classes(dim),
        }
        for dim in dimensions
        if desks_by_dim.get(dim)
    }

    stats = {
        "scorecards_seen": valid_rows,
        "desks_seen": len(by_desk),
        "starved_desks": [
            {"desk": desk, "starved_dim_count": count}
            for desk, count in sorted(
                starved_by_desk.items(), key=lambda kv: (-kv[1], kv[0])
            )
        ],
        "by_dimension": by_dimension,
    }
    return gaps, stats


# ---------------------------------------------------------------------------
# Finding assembly
# ---------------------------------------------------------------------------


def _build_finding(
    gaps: list[dict[str, Any]], stats: Mapping[str, Any]
) -> FindingPayload:
    n = len(gaps)
    starved_desks = stats.get("starved_desks") or []
    n_desks = len(starved_desks)
    if n:
        title = (
            f"Collection requirements: {n} starved cell(s) across "
            f"{n_desks} desk(s)"
        )
        lines: list[str] = []
        for g in gaps[:_MAX_GAPS_IN_FINDING]:
            classes = ", ".join(g.get("source_classes") or []) or "(none)"
            persistence = g.get("persistence")
            persist = (
                f" [{g['insufficient_count']}/{g['window_scorecards']}"
                f"={persistence}]"
                if g.get("window_scorecards")
                else ""
            )
            lines.append(
                f"- {g['desk']} / {g['dimension']}: {g['reason']}{persist} "
                f"→ feed: {classes}"
            )
        body = "\n".join(lines)
    else:
        title = "Collection requirements: no starved cells"
        body = (
            "No desk×dimension cell is insufficient in the current scorecards "
            "this sweep."
        )
    tags = ["deterministic", SUB_HANDLER_NAME]
    if n:
        tags.append("collection_gap")
    return FindingPayload(
        title=title[:2048],
        body=body[:65536],
        confidence=1.0,
        evidence=[],
        tags=tags,
        data={
            "sub_handler": SUB_HANDLER_NAME,
            "gap_count": n,
            "starved_desk_count": n_desks,
            "scorecards_seen": stats.get("scorecards_seen", 0),
            "desks_seen": stats.get("desks_seen", 0),
            "starved_desks": starved_desks,
            "by_dimension": stats.get("by_dimension", {}),
            "gaps": gaps[:_MAX_GAPS_IN_FINDING],
        },
    )


# ---------------------------------------------------------------------------
# Live-pool path (asyncpg)
# ---------------------------------------------------------------------------


_FETCH_SCORECARDS_SQL = """
    SELECT id::text AS id, target_id, produced_at, data
      FROM analyst_outputs
     WHERE kind = 'scorecard'
       AND produced_at > NOW() - make_interval(days => $1)
     ORDER BY produced_at DESC, id DESC
"""


async def _fetch_scorecards(
    conn: Any, *, window_days: int
) -> list[dict[str, Any]]:
    """All scorecard rows produced within the window (HEAD + superseded).

    NO ``superseded_by IS NULL`` filter: the aggregation needs every card in the
    window to count how persistently a cell has been insufficient. The latest
    card per desk (picked in :func:`aggregate_gaps`) decides current starvation.
    """
    rows = await conn.fetch(_FETCH_SCORECARDS_SQL, int(window_days))
    return [
        {
            "id": r["id"],
            "target_id": r["target_id"],
            "produced_at": r["produced_at"],
            "data": r["data"],
        }
        for r in rows
    ]


async def _last_emitted_body(pool: Any, analyst_id: str) -> str | None:
    """Body of the most recent FEED finding this analyst emitted (or None).

    Trace-only suppressed runs write no ``analyst_outputs`` row, so this is the
    last NON-suppressed summary — exactly what a re-swept identical gap set
    should be deduped against.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT body FROM analyst_outputs "
            "WHERE analyst_id = $1 AND kind = 'finding' "
            "ORDER BY produced_at DESC LIMIT 1",
            analyst_id,
        )
    return row["body"] if row else None


# ---------------------------------------------------------------------------
# Public handler entry point
# ---------------------------------------------------------------------------


async def handle(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: Any | None,
) -> AnalystMethodResult:
    """Sub-handler entry point — see the module docstring.

    ``deps`` is the analyst pool bundle (``deps.pg_pool``); ``deps=None`` runs the
    synthetic path (aggregates pre-shaped ``inputs`` scorecard rows, no DB) for
    unit tests.

    Options
    -------
    window_days:
        Only scorecards this recent are aggregated (default 35).
    analyst_id:
        This analyst's own id (used to scope the last-emitted-body dedup lookup).
    """
    window_days = int(options.get("window_days", _DEFAULT_WINDOW_DAYS))
    pool = getattr(deps, "pg_pool", None) if deps is not None else None

    if pool is not None:
        try:
            async with pool.acquire() as conn:
                rows = await _fetch_scorecards(conn, window_days=window_days)
            gaps, stats = aggregate_gaps(rows)
        except Exception as exc:  # noqa: BLE001 — degrade-not-drop
            logger.warning("collection_gap.pool_failed err=%s", exc)
            gaps, stats = [], {"scorecards_seen": 0, "desks_seen": 0,
                               "starved_desks": [], "by_dimension": {}}
    else:
        gaps, stats = aggregate_gaps([dict(r) for r in inputs])

    finding = _build_finding(gaps, stats)

    # Emit a FEED finding only when there is a starved cell to surface. A no-gap
    # sweep is suppressed (trace-only) so an idempotent monthly re-run does not
    # repeat 'nothing starved'; and on the live path a gap set byte-identical to
    # the last EMITTED summary (the same scorecards re-swept) is likewise
    # suppressed. Degrade to emit on any dedup-check failure.
    analyst_id = str(options.get("analyst_id") or SUB_HANDLER_NAME)
    force_trace_only = not gaps
    if gaps and pool is not None:
        try:
            force_trace_only = (
                await _last_emitted_body(pool, analyst_id) == finding.body
            )
        except Exception as exc:  # noqa: BLE001 — degrade: emit rather than crash
            logger.warning("collection_gap.dedup_check_failed err=%s", exc)
            force_trace_only = False

    return AnalystMethodResult(
        finding=finding,
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
        force_trace_only=force_trace_only,
    )


__all__ = ["handle", "aggregate_gaps", "SOURCE_CLASSES_BY_DIMENSION", "SUB_HANDLER_NAME"]
