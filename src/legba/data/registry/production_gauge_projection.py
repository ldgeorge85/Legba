# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P4b S-1 closed loop — the graph-projection parity/freshness gauge.

One :class:`LoopGauge` row (``graph_projection:graph_arcs``) answering the
question the judge's §4.2 design exists to make answerable: *is the
disposable projection actually being rebuilt, and did it see every source?*

The loop reads ``graph_arcs_meta`` (the one-row build receipt) plus a
``pg_class.reltuples`` estimate per source table — estimates, never
``count(*)``, because the gauge is a per-request read and the parity question
it answers ("a source table has rows but contributed zero arcs") does not
need exact counts to be honest about.

What it reports:

  * **staleness** — ``age_seconds`` vs ``LEGBA_GRAPH_PROJECTION_MAX_AGE_
    SECONDS``; the ratio rides the shared severity ramp.
  * **consistency** — ``sum(source_counts)`` vs ``arc_count``; a divergence
    means the receipt is corrupt, which is a different (and worse) problem
    than a stale one.
  * **missing legs** — source tables with live rows that contributed zero
    arcs to the last build: a broken per-source SELECT surfaces here, not in
    a reader's mysteriously-thin walk.
  * **build_seconds / arc_count** — evidence, the same numbers the E4 trigger
    publishes (one build receipt, two readers).

Quiet-by-design: flag off → ``ungauged`` with ``projection_disabled`` (a
deliberately-off plane is a configuration, not a deficit). Flag on with no
receipt → deficit: a build was expected and none landed. Degrades loud — a
failed read is ``ungauged`` carrying the error.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from ..graph_projection import (
    GRAPH_ARC_SOURCE_TABLES,
    GUARANTEED_ARC_SOURCES,
    PROJECTION_DISABLED,
    PROJECTION_EMPTY,
    graph_projection_enabled,
    projection_max_age_seconds,
)
from .production_gauge import (
    GaugeConfig,
    LoopGauge,
    _ungauged,
    severity_for_ratio,
)

logger = logging.getLogger(__name__)

#: Deploy-marker convention (GRAPH_TRIGGER_GAUGE_VERSION etc.).
GRAPH_PROJECTION_GAUGE_VERSION = "2026-09/p4b"

LOOP_GRAPH_PROJECTION = "graph_projection"
LOOP_ID = "graph_arcs"
LABEL = "Graph projection parity + freshness"

#: Quiet-by-design reasons this loop adds to the S-1 vocabulary.
QUIET_PROJECTION_DISABLED = PROJECTION_DISABLED
QUIET_NO_RECEIPT = "projection_empty"  # flag on, no build ever landed
QUIET_META_QUERY_FAILED = "projection_meta_query_failed"

_META_SQL = """
SELECT projected_at, arc_count, source_counts, build_seconds
  FROM public.graph_arcs_meta
 WHERE id
"""

#: Planner estimate per source table — instant, approximate, enough for the
#: "rows but zero arcs" leg-detector.
_RELTUPLES_SQL = """
SELECT relname, reltuples::bigint AS estimate
  FROM pg_class
 WHERE relname = ANY($1::text[])
"""


def projection_gauge(
    meta: Mapping[str, Any] | None,
    estimates: Mapping[str, int],
    *,
    now: datetime,
    cfg: GaugeConfig,
) -> LoopGauge:
    """Judge one build receipt against the live source tables."""
    if meta is None:
        return LoopGauge(
            loop_class=LOOP_GRAPH_PROJECTION,
            loop_id=LOOP_ID,
            label=LABEL,
            state="deficit",
            severity="high",
            ratio=None,
            expected="a completed graph_arcs build within the descriptor cadence",
            actual="no build receipt — the projector has never landed one",
            quiet_reason=None,
            evidence={"reason": QUIET_NO_RECEIPT},
        )

    projected_at = meta.get("projected_at")
    if projected_at is not None and getattr(projected_at, "tzinfo", None) is None:
        projected_at = projected_at.replace(tzinfo=timezone.utc)
    age = (
        (now - projected_at).total_seconds() if projected_at is not None else None
    )
    max_age = projection_max_age_seconds()
    arc_count = int(meta.get("arc_count") or 0)
    raw_counts = meta.get("source_counts") or {}
    if isinstance(raw_counts, str):  # asyncpg returns jsonb as str
        try:
            raw_counts = json.loads(raw_counts)
        except (ValueError, TypeError):
            raw_counts = {}
    if not isinstance(raw_counts, Mapping):
        raw_counts = {}
    source_counts = {str(k): int(v or 0) for k, v in raw_counts.items()}
    build_seconds = meta.get("build_seconds")

    # Leg detector: a GUARANTEED-arc source (junction/link tables — every row
    # must produce an arc) with live rows that contributed zero. Conditional
    # sources (entity_profiles w/o merges, facts w/o resolvable names, …) are
    # legitimately empty and are never flagged.
    missing_legs = sorted(
        t
        for t in GUARANTEED_ARC_SOURCES
        if int(estimates.get(t) or 0) > 0 and source_counts.get(t, 0) == 0
    )
    count_sum = sum(source_counts.values())
    consistent = count_sum == arc_count

    evidence: dict[str, Any] = {
        "arc_count": arc_count,
        "source_counts": source_counts,
        "build_seconds": build_seconds,
        "age_seconds": age,
        "max_age_seconds": max_age,
        "missing_legs": missing_legs,
        "consistent": consistent,
    }

    if not consistent:
        return LoopGauge(
            loop_class=LOOP_GRAPH_PROJECTION,
            loop_id=LOOP_ID,
            label=LABEL,
            state="deficit",
            severity="high",
            ratio=None,
            expected="sum(source_counts) == arc_count",
            actual=f"sum={count_sum} vs arc_count={arc_count}",
            last_production_at=projected_at,
            evidence={**evidence, "note": "the build receipt disagrees with "
                     "itself — trust neither half"},
        )
    if missing_legs:
        return LoopGauge(
            loop_class=LOOP_GRAPH_PROJECTION,
            loop_id=LOOP_ID,
            label=LABEL,
            state="deficit",
            severity="medium",
            ratio=None,
            expected="every non-empty source table contributes arcs",
            actual=f"{len(missing_legs)} sources contributed zero: "
                   f"{missing_legs}",
            last_production_at=projected_at,
            evidence=evidence,
        )
    if age is not None and age > max_age:
        ratio = age / max_age
        return LoopGauge(
            loop_class=LOOP_GRAPH_PROJECTION,
            loop_id=LOOP_ID,
            label=LABEL,
            state="deficit",
            severity=severity_for_ratio(ratio),
            ratio=round(ratio, 3),
            expected=f"a rebuild within {max_age:.0f}s",
            actual=f"last build {age:.0f}s ago",
            last_production_at=projected_at,
            evidence=evidence,
        )
    return LoopGauge(
        loop_class=LOOP_GRAPH_PROJECTION,
        loop_id=LOOP_ID,
        label=LABEL,
        state="ok",
        ratio=0.0,
        expected=f"a rebuild within {max_age:.0f}s covering every source",
        actual=(
            f"{arc_count} arcs across {len(source_counts)} sources, "
            f"built in {build_seconds}s, age {age:.0f}s"
            if age is not None
            else f"{arc_count} arcs (projected_at missing)"
        ),
        last_production_at=projected_at,
        evidence=evidence,
    )


async def read_projection_loops(
    conn: Any, *, now: datetime, cfg: GaugeConfig
) -> list[LoopGauge]:
    """The one-row projection gauge. READ-ONLY; degrades loud."""
    if not graph_projection_enabled():
        return [
            _ungauged(
                LOOP_GRAPH_PROJECTION,
                LOOP_ID,
                LABEL,
                QUIET_PROJECTION_DISABLED,
            )
        ]
    try:
        row = await conn.fetchrow(_META_SQL)
        est_rows = await conn.fetch(
            _RELTUPLES_SQL, list(GRAPH_ARC_SOURCE_TABLES)
        )
    except Exception as exc:  # pragma: no cover — degrade-not-drop
        logger.warning(
            "production_gauge.projection_meta_query_failed err=%s", exc
        )
        return [
            _ungauged(
                LOOP_GRAPH_PROJECTION,
                LOOP_ID,
                LABEL,
                QUIET_META_QUERY_FAILED,
                error=str(exc)[:300],
            )
        ]
    estimates = {
        str(r["relname"]): int(r["estimate"] or 0) for r in (est_rows or [])
    }
    return [
        projection_gauge(
            dict(row) if row is not None else None,
            estimates,
            now=now,
            cfg=cfg,
        )
    ]


__all__ = [
    "LOOP_GRAPH_PROJECTION",
    "QUIET_META_QUERY_FAILED",
    "QUIET_NO_RECEIPT",
    "QUIET_PROJECTION_DISABLED",
    "projection_gauge",
    "read_projection_loops",
]
