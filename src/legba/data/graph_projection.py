# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P4b — the ``LEGBA_GRAPH_PROJECTION`` flag and the shared staleness read.

A LEAF module (stdlib + typing only) so both sides can import it: the
``graph_projector`` deterministic handler (full image) and the
``graph_arcs_api`` registry route (slim image) read the SAME flag helper and
the SAME projection-state query — one implementation, never a mirror that
drifts.

The kill-switch contract (spec §4.2 rule 5): flag off means the table is not
built, the builder does not run, and every reader answers
``projection_disabled``. The staleness contract (rule 4):
``graph_arcs_meta.projected_at`` is published on every read and a reader past
``LEGBA_GRAPH_PROJECTION_MAX_AGE_SECONDS`` refuses with ``projection_stale``;
an empty projection answers ``projection_empty`` — never ``found=False``.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Literal

#: The kill switch (spec §4.2 rule 5). Default OFF.
GRAPH_PROJECTION_ENV = "LEGBA_GRAPH_PROJECTION"

#: The staleness ceiling a reader tolerates before refusing. The projector
#: descriptor runs hourly; the default gives one missed tick of grace.
GRAPH_PROJECTION_MAX_AGE_ENV = "LEGBA_GRAPH_PROJECTION_MAX_AGE_SECONDS"
DEFAULT_MAX_AGE_SECONDS = 7_200.0

#: The refusal vocabulary. These strings are the API's `state` field and the
#: S-1 loop's quiet reasons — one vocabulary, every reader.
PROJECTION_DISABLED = "projection_disabled"
PROJECTION_STALE = "projection_stale"
PROJECTION_EMPTY = "projection_empty"
PROJECTION_OK = "ok"

ProjectionState = Literal[
    "projection_disabled", "projection_stale", "projection_empty", "ok"
]

#: The authoritative source tables the P4b projector reads — every
#: ``src_table`` value that can appear in ``graph_arcs`` /
#: ``graph_arcs_meta.source_counts``. Kept in this leaf so the projector, the
#: API and the S-1 parity gauge share ONE vocabulary; the drift guard in
#: ``test_graph_projector.py`` asserts the projector's ``_SOURCES`` key set
#: stays equal to this tuple.
GRAPH_ARC_SOURCE_TABLES: tuple[str, ...] = (
    "signal_entity_links",
    "signal_aliases",
    "signals",
    "signal_event_links",
    "entity_edges",
    "event_entity_links",
    "event_edges",
    "situation_event_links",
    "facts",
    "fact_contention_values",
    "narrative_echo_edges",
    "situations",
    "situation_events",
    "analyst_outputs",
    "output_consumption",
    "bearing_edges",
    "entity_profiles",
)

#: The subset of :data:`GRAPH_ARC_SOURCE_TABLES` where EVERY source row is
#: guaranteed to produce at least one arc — junction/link tables plus
#: ``entity_edges`` (the main arc; the via hops are additive). The S-1 parity
#: gauge's missing-leg detector may only flag THESE: a conditional source
#: (``entity_profiles`` with no merges, ``analyst_outputs`` with empty
#: lineage, ``facts`` whose names resolve to nothing) legitimately
#: contributes zero arcs from a non-empty table, and flagging it would
#: manufacture a permanent false deficit.
GUARANTEED_ARC_SOURCES: tuple[str, ...] = (
    "signal_entity_links",
    "signal_aliases",
    "signal_event_links",
    "entity_edges",
    "event_entity_links",
    "event_edges",
    "situation_event_links",
    "situation_events",
    "output_consumption",
    "bearing_edges",
    "narrative_echo_edges",
)

#: Reads the one-row build receipt. Schema-qualified on purpose: the
#: AGE-equipped pool sets ``search_path = ag_catalog, "$user", public``.
_META_SQL = """
SELECT projected_at, arc_count, source_counts, build_seconds
  FROM public.graph_arcs_meta
 WHERE id
"""


def graph_projection_enabled() -> bool:
    """Honor ``LEGBA_GRAPH_PROJECTION`` (default OFF) — the P4b kill switch.

    Same contract as ``events_enabled``: only "1"/"true"/"yes"/"on" enable it;
    unset/empty/anything-else keeps it off. Off means the projection is never
    built and every reader refuses distinctly.
    """
    raw = os.environ.get(GRAPH_PROJECTION_ENV, "0").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def projection_max_age_seconds() -> float:
    """``LEGBA_GRAPH_PROJECTION_MAX_AGE_SECONDS`` — how stale a build may be
    before readers refuse with ``projection_stale`` (default 7200 s)."""
    raw = os.environ.get(GRAPH_PROJECTION_MAX_AGE_ENV, "").strip()
    if not raw:
        return DEFAULT_MAX_AGE_SECONDS
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_MAX_AGE_SECONDS


async def projection_state(
    conn: Any, *, now: datetime | None = None
) -> tuple[ProjectionState, dict[str, Any] | None]:
    """The shared refusal read — flag, build presence, staleness, emptiness.

    Returns ``(state, meta)``: ``meta`` is the ``graph_arcs_meta`` row as a
    dict when one exists. The order is the contract: a disabled projection
    never opens the table; an unbuilt or empty one is ``projection_empty``; a
    built one past its max age is ``projection_stale``; anything else is
    ``ok`` and the caller may read arcs.
    """
    if not graph_projection_enabled():
        return PROJECTION_DISABLED, None
    try:
        row = await conn.fetchrow(_META_SQL)
    except Exception:
        # Table absent (migration not applied) reads as never-built, not as a
        # crash — the deployable-with-flags-off contract.
        return PROJECTION_EMPTY, None
    if row is None or int(row["arc_count"] or 0) <= 0:
        return PROJECTION_EMPTY, dict(row) if row is not None else None
    meta = dict(row)
    at = now or datetime.now(timezone.utc)
    projected_at = meta.get("projected_at")
    if projected_at is not None and projected_at.tzinfo is None:
        projected_at = projected_at.replace(tzinfo=timezone.utc)
        meta["projected_at"] = projected_at
    if projected_at is not None:
        age = (at - projected_at).total_seconds()
        meta["age_seconds"] = age
        if age > projection_max_age_seconds():
            return PROJECTION_STALE, meta
    return PROJECTION_OK, meta


__all__ = [
    "DEFAULT_MAX_AGE_SECONDS",
    "GRAPH_ARC_SOURCE_TABLES",
    "GUARANTEED_ARC_SOURCES",
    "GRAPH_PROJECTION_ENV",
    "GRAPH_PROJECTION_MAX_AGE_ENV",
    "PROJECTION_DISABLED",
    "PROJECTION_EMPTY",
    "PROJECTION_OK",
    "PROJECTION_STALE",
    "ProjectionState",
    "graph_projection_enabled",
    "projection_max_age_seconds",
    "projection_state",
]
