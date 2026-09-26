# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``GET /v3/graph-triggers`` — the five pre-registered graph-engine gauges.

DATA MODEL V3 / P4a. The graph-engine decision was deferred ONCE, to the
2026-11-03 sitting, on five pre-registered triggers (the judge's §4.2 table;
the spec's §4.4 restatement): an engine is warranted when **any two** of E1-E5
fire. This route exists so that on that date each trigger is a READING, not an
argument — and so that today, two months out, it is already visible which
gauges cannot yet be read.

The honesty contract is the whole point of the phase: a gauge that cannot be
computed reports ``state="unreadable"`` with the reason named and
``reading=null`` — **never** a 0 that a reader could mistake for "far below
threshold". Three states:

  * ``measured``   — the gauge computed; ``reading``/``distance``/``fired``
                     are real.
  * ``unreadable`` — the instrument is missing or the sample is too thin;
                     ``unreadable_reason`` says which. E2 below 100 timed
                     invocations, E3's shape leg (no shape classifier exists),
                     and E4 until the P4b projector ships are all here.
  * ``declared``   — E5 is a product commitment, not a meter; it reports the
                     current product position as prose in ``position``.

NO MIGRATION of its own: it reads ``entity_edges`` (E1),
``action_pack_invocations.duration_ms`` (E2 — the column 0208 adds),
``action_pack_invocations`` (E3), and ``graph_arcs_meta`` (E4, gated on
``to_regclass`` — the table lands with P4b's 0207 and the route must answer
honestly before it exists).

Registry-slim: stdlib + fastapi + pydantic + ``.api`` only. Nothing from
``legba.data.analysts`` or ``legba.runtime`` — this module ships in the slim
registry image.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from .api import RegistryAPIDeps, require_bearer

logger = logging.getLogger(__name__)

GRAPH_TRIGGER_GAUGE_VERSION = "2026-09/p4a"

_ROUTE = "/graph-triggers"

#: The date the deferred engine decision is re-read — JUDGE_SYNTHESIS §4.2's
#: "re-evaluate the deferral on a calendar" clause (90 days from the verdict).
SITTING_DATE = "2026-11-03"

#: The pre-registered rule, restated on the wire so the reading cannot be
#: mistaken for "one gauge firing buys an engine".
DECISION_RULE = (
    "An engine is warranted when any TWO of E1-E5 fire (pre-registered, "
    "JUDGE_SYNTHESIS 2026-08 §4.2)."
)

# --- thresholds, verbatim from JUDGE_SYNTHESIS §4.2 --------------------------

#: E1 — open ``edge_family='relation'`` edges (derived only: NOT reference,
#: NOT cooccurrence). The open-row predicate is entity_edges' own:
#: ``valid_until IS NULL AND superseded_by IS NULL``.
E1_OPEN_RELATION_EDGES = 250_000

#: E2 — p95 wall-clock latency of the shipped graph tools, over a rolling
#: window, is only a reading once the sample is real.
E2_P95_MS = 2_000
E2_MIN_INVOCATIONS = 100
E2_WINDOW_DAYS = 30

#: E3 — sustained demand for shapes the typed SQL builder cannot express
#: (>=4 hops, or per-hop type + temporal predicates): the demand leg is
#: countable; the shape leg has no classifier and stays unreadable.
E3_INVOCATIONS_PER_DAY = 20.0
E3_WINDOW_DAYS = 14
E3_SHAPE_SHARE = 0.20

#: E4 — full in-process snapshot rebuild. Read off ``graph_arcs_meta`` once
#: the P4b projector ships; until then the gauge is explicitly unreadable.
E4_REBUILD_SECONDS = 60.0

#: The tools the gauge calls "the shipped graph tools": the three deep-walk
#: tools (E3's numerator — the shapes a typed builder cannot express live
#: there) plus the structural readers the judge's demand count already
#: covered (inspect_entity / query_nexuses / query_facts) and the journal
#: pack's structural-balance walk. A tool name here must exist on a live
#: pack's ``tools:`` list — a name nothing can invoke would silently keep the
#: gauge at n=0.
GRAPH_TOOLS: tuple[str, ...] = (
    "query_paths",
    "find_proxy_chains",
    "query_brokers",
    "query_nexuses",
    "inspect_entity",
    "query_facts",
    "get_structural_balance",
)

#: E3's numerator — the deep-walk subset. The other four measure demand for
#: shapes the builder already expresses; they do not count toward E3.
WALK_TOOLS: tuple[str, ...] = (
    "query_paths",
    "find_proxy_chains",
    "query_brokers",
)


# ---------------------------------------------------------------------------
# Wire shapes
# ---------------------------------------------------------------------------


class TriggerGauge(BaseModel):
    """One pre-registered trigger, with its reading — or why there is none."""

    id: str
    #: Plain-language statement of what fires this trigger.
    question: str
    #: ``measured`` | ``unreadable`` | ``declared`` — see the module docstring.
    state: str
    #: The threshold in words — "over 250,000 open relation edges" — so a
    #: reading can never be detached from the bar it is read against.
    threshold: str
    #: The gauge's number. ``null`` when ``state != "measured"`` — an
    #: unreadable gauge reports no number rather than a 0 that reads as
    #: "safely below".
    reading: Optional[float] = None
    #: Unit of ``reading`` ("edges", "ms", "invocations/day", "seconds").
    reading_unit: str = ""
    #: How far the reading sits from the threshold, in words.
    distance: Optional[str] = None
    #: Whether the trigger has fired. ``null`` whenever any leg needed for the
    #: verdict is unreadable — never a guess.
    fired: Optional[bool] = None
    #: Why the gauge cannot be read, when ``state == "unreadable"``.
    unreadable_reason: Optional[str] = None
    #: E5 only: the current product position, stated as prose.
    position: Optional[str] = None
    #: The raw numbers / provenance the reading was computed from.
    evidence: dict[str, Any] = Field(default_factory=dict)


class GraphTriggersOut(BaseModel):
    """The five-gauge read for the 2026-11-03 sitting."""

    generated_at: datetime
    sitting: str = SITTING_DATE
    rule: str = DECISION_RULE
    gauge_version: str = GRAPH_TRIGGER_GAUGE_VERSION
    #: False when the substrate could not be read at all — an empty gauge is
    #: "we could not measure", never "all clear".
    measured: bool = False
    triggers: list[TriggerGauge] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# SQL — kept inline and explicit; this module is the ONLY reader of these.
# ---------------------------------------------------------------------------

_E1_SQL = """
SELECT count(*)
FROM entity_edges
WHERE edge_family = 'relation'
  AND valid_until IS NULL
  AND superseded_by IS NULL
"""

#: percentile_cont is an ordered-set aggregate and ignores NULL inputs, so
#: pre-0208 rows (duration_ms IS NULL) cannot drag the p95 — but they also
#: cannot be counted toward n, which is why n counts only timed rows.
_E2_SQL = """
SELECT count(*) FILTER (WHERE duration_ms IS NOT NULL)            AS n_timed,
       count(*)                                                   AS n_total,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms)  AS p95_ms,
       max(duration_ms)                                           AS max_ms
FROM action_pack_invocations
WHERE tool_name = ANY($1::text[])
  AND occurred_at > now() - make_interval(days => $2)
"""

_E3_SQL = """
SELECT tool_name, count(*) AS n
FROM action_pack_invocations
WHERE tool_name = ANY($1::text[])
  AND occurred_at > now() - make_interval(days => $2)
GROUP BY tool_name
"""

_E4_TABLE_SQL = "SELECT to_regclass('public.graph_arcs_meta')"

#: Schema-qualified on purpose: the AGE-equipped pool sets
#: ``search_path = ag_catalog, "$user", public`` (postgres.py
#: ``_setup_connection``), so an unqualified name can resolve into ag_catalog.
_E4_SQL = """
SELECT build_seconds, arc_count, projected_at, source_counts
FROM public.graph_arcs_meta
WHERE id
"""


# ---------------------------------------------------------------------------
# Gauges — one builder per trigger. Each takes the rows the route fetched and
# returns the gauge; none touches the DB itself (testable pure shape).
# ---------------------------------------------------------------------------


def _distance_below(reading: float, threshold: float, unit: str) -> str:
    """Human distance-to-fire for a 'fires ABOVE threshold' gauge."""
    if threshold <= 0:
        return ""
    if reading <= 0:
        return f"no reading against the {threshold:,.0f} {unit} threshold"
    ratio = reading / threshold
    if ratio >= 1:
        return f"{ratio:.1f}x OVER the {threshold:,.0f} {unit} threshold"
    return f"{ratio:.1%} of the {threshold:,.0f} {unit} threshold ({1/ratio:.1f}x below)"


def gauge_e1(open_relation_edges: int) -> TriggerGauge:
    """E1 — open derived (relation-family) entity edges > 250,000."""
    reading = float(open_relation_edges)
    return TriggerGauge(
        id="E1",
        question=(
            "Open edge_family='relation' entity edges (derived only — not "
            "reference, not cooccurrence)."
        ),
        state="measured",
        threshold=f"> {E1_OPEN_RELATION_EDGES:,} open relation edges",
        reading=reading,
        reading_unit="edges",
        distance=_distance_below(reading, E1_OPEN_RELATION_EDGES, "edge"),
        fired=reading > E1_OPEN_RELATION_EDGES,
        evidence={"open_relation_edges": open_relation_edges},
    )


def gauge_e2(stats: dict[str, Any], *, window_days: int) -> TriggerGauge:
    """E2 — p95 invocation latency of the shipped graph tools, 30d window.

    Below ``E2_MIN_INVOCATIONS`` timed invocations the p95 is not a reading —
    it is noise over a handful of calls — so the gauge reports
    ``unreadable: insufficient invocations`` with the n it saw.
    """
    n_timed = int(stats.get("n_timed") or 0)
    evidence = {
        "tools": list(GRAPH_TOOLS),
        "window_days": window_days,
        "invocations_timed": n_timed,
        "invocations_total": int(stats.get("n_total") or 0),
        "min_invocations": E2_MIN_INVOCATIONS,
    }
    if n_timed < E2_MIN_INVOCATIONS:
        return TriggerGauge(
            id="E2",
            question=(
                "p95 latency of the shipped graph tools over >= "
                f"{E2_MIN_INVOCATIONS} real invocations in a rolling "
                f"{window_days} days."
            ),
            state="unreadable",
            threshold=f"> {E2_P95_MS:,} ms p95",
            reading=None,
            reading_unit="ms",
            unreadable_reason=(
                f"insufficient invocations (n={n_timed} timed of "
                f"{E2_MIN_INVOCATIONS} required in {window_days}d)"
            ),
            evidence=evidence,
        )
    p95 = float(stats["p95_ms"])
    evidence["p95_ms"] = p95
    evidence["max_ms"] = stats.get("max_ms")
    return TriggerGauge(
        id="E2",
        question=(
            "p95 latency of the shipped graph tools over >= "
            f"{E2_MIN_INVOCATIONS} real invocations in a rolling "
            f"{window_days} days."
        ),
        state="measured",
        threshold=f"> {E2_P95_MS:,} ms p95",
        reading=p95,
        reading_unit="ms",
        distance=_distance_below(p95, E2_P95_MS, "ms p95"),
        fired=p95 > E2_P95_MS,
        evidence=evidence,
    )


def gauge_e3(tool_counts: dict[str, int], *, window_days: int) -> TriggerGauge:
    """E3 — sustained deep-walk demand needing shapes the builder can't express.

    The demand leg (invocations/day over the walk tools) is countable today.
    The shape leg — "and >=20% such shapes" — has no classifier and is carried
    as an explicit unreadable sub-gauge in ``evidence``; the trigger can only
    be declared fired when BOTH legs read, so ``fired`` is ``False`` while
    demand is under threshold and ``null`` if demand ever crosses it with the
    shape leg still dark.
    """
    total = sum(tool_counts.values())
    per_day = total / window_days if window_days else 0.0
    demand_fired = per_day >= E3_INVOCATIONS_PER_DAY
    return TriggerGauge(
        id="E3",
        question=(
            "Sustained graph-tool demand needing shapes the typed SQL builder "
            "cannot express (>=4 hops, or per-hop type + temporal predicates)."
        ),
        state="measured",
        threshold=(
            f">= {E3_INVOCATIONS_PER_DAY:g} invocations/day for "
            f"{window_days} days AND >= {E3_SHAPE_SHARE:.0%} such shapes"
        ),
        reading=per_day,
        reading_unit="invocations/day",
        distance=_distance_below(
            per_day, E3_INVOCATIONS_PER_DAY, "invocations/day"
        ),
        # The AND can only evaluate False while the demand leg is under; once
        # demand crosses, the unreadable shape leg leaves the verdict null.
        fired=(False if not demand_fired else None),
        # The demand leg IS read — the unreadable half is the shape leg, and
        # it is declared unreadable in evidence.shape_share, not here.
        unreadable_reason=None,
        evidence={
            "tools": dict(tool_counts),
            "window_days": window_days,
            "invocations_in_window": total,
            "shape_share": {
                "state": "unreadable",
                "reason": "no shape classifier",
                "threshold": E3_SHAPE_SHARE,
            },
        },
    )


def gauge_e4(meta_row: Optional[dict[str, Any]], *, table_present: bool) -> TriggerGauge:
    """E4 — full in-process snapshot rebuild > 60 s.

    Read off ``graph_arcs_meta.build_seconds``, the receipt the P4b projector
    writes per build. Until 0207 exists and the projector has run once, the
    gauge is ``unreadable: projector not deployed`` — the judge's ~8 us/edge
    extrapolation (60 s at ~7.8 M edges) is carried in evidence as context,
    never as the reading.
    """
    question = (
        "Full in-process snapshot rebuild of the projected arc set "
        "(graph_arcs_meta.build_seconds)."
    )
    if not table_present or meta_row is None:
        return TriggerGauge(
            id="E4",
            question=question,
            state="unreadable",
            threshold=f"> {E4_REBUILD_SECONDS:g} s rebuild",
            reading=None,
            reading_unit="seconds",
            unreadable_reason="projector not deployed",
            evidence={
                "graph_arcs_meta_present": table_present,
                # AGE_PROBE_REPORT §5.2's measured ~8 us/edge — the reason the
                # sitting cares about this number, quoted as extrapolation.
                "extrapolation": (
                    "~8 us/edge (AGE_PROBE_REPORT 2026-08-03 §5.2): 60 s at "
                    "~7.8M arcs; ~32 s at the ~4.0M-arc projection"
                ),
            },
        )
    build_seconds = float(meta_row["build_seconds"])
    return TriggerGauge(
        id="E4",
        question=question,
        state="measured",
        threshold=f"> {E4_REBUILD_SECONDS:g} s rebuild",
        reading=build_seconds,
        reading_unit="seconds",
        distance=_distance_below(build_seconds, E4_REBUILD_SECONDS, "s"),
        fired=build_seconds > E4_REBUILD_SECONDS,
        evidence={
            "arc_count": meta_row.get("arc_count"),
            "projected_at": (
                meta_row["projected_at"].isoformat()
                if isinstance(meta_row.get("projected_at"), datetime)
                else meta_row.get("projected_at")
            ),
        },
    )


def gauge_e5() -> TriggerGauge:
    """E5 — interactive (<2 s) attribute-rich centrality/community required.

    A product commitment, not a meter: it fires when the operator commits to
    interactive graph algorithms. Reported as the current position, never as
    a number.
    """
    return TriggerGauge(
        id="E5",
        question=(
            "Attribute-rich centrality/community required interactive "
            "(<2 s) rather than on cadence."
        ),
        state="declared",
        threshold="any product commitment to interactive (<2 s) algorithms",
        reading=None,
        fired=None,
        position=(
            "No interactive requirement is committed: the graph analytics "
            "(structural_balance, graph_mining) run on cadence, and "
            "graph_mining emits modularity=NULL. An interactive product "
            "commitment is declared at a sitting, not measured here."
        ),
        evidence={},
    )


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def build_graph_triggers_router(deps: RegistryAPIDeps) -> APIRouter:
    router = APIRouter(tags=["system"])

    def _get_deps(request: Request) -> RegistryAPIDeps:
        return getattr(request.app.state, "registry_deps", deps)

    @router.get(_ROUTE, response_model=GraphTriggersOut)
    async def v3_graph_triggers(
        _principal: str = Depends(require_bearer),
        deps_: RegistryAPIDeps = Depends(_get_deps),
    ) -> GraphTriggersOut:
        """The five pre-registered engine triggers, each read or unreadable.

        One substrate pass: E1's edge count, E2's 30d latency sample, E3's
        14d demand window, and E4's projector receipt (``to_regclass``-gated
        so a deployment before P4b answers honestly rather than erroring).
        """
        now = datetime.now(timezone.utc)
        try:
            async with deps_.descriptor_registry.pg.acquire() as conn:
                e1_count = int(await conn.fetchval(_E1_SQL) or 0)
                e2_stats = dict(
                    await conn.fetchrow(
                        _E2_SQL, list(GRAPH_TOOLS), E2_WINDOW_DAYS
                    ) or {}
                )
                e3_rows = await conn.fetch(
                    _E3_SQL, list(WALK_TOOLS), E3_WINDOW_DAYS
                )
                has_meta = await conn.fetchval(_E4_TABLE_SQL)
                e4_row = None
                if has_meta:
                    e4_row = await conn.fetchrow(_E4_SQL)
        except Exception as exc:  # noqa: BLE001 — a polled surface never 500s
            logger.info("v3.graph_triggers.unavailable err=%s", exc)
            return GraphTriggersOut(generated_at=now)

        return GraphTriggersOut(
            generated_at=now,
            measured=True,
            triggers=[
                gauge_e1(e1_count),
                gauge_e2(e2_stats, window_days=E2_WINDOW_DAYS),
                gauge_e3(
                    {str(r["tool_name"]): int(r["n"]) for r in e3_rows},
                    window_days=E3_WINDOW_DAYS,
                ),
                gauge_e4(
                    dict(e4_row) if e4_row is not None else None,
                    table_present=bool(has_meta),
                ),
                gauge_e5(),
            ],
        )

    return router


__all__ = [
    "DECISION_RULE",
    "E1_OPEN_RELATION_EDGES",
    "E2_MIN_INVOCATIONS",
    "E2_P95_MS",
    "E2_WINDOW_DAYS",
    "E3_INVOCATIONS_PER_DAY",
    "E3_SHAPE_SHARE",
    "E3_WINDOW_DAYS",
    "E4_REBUILD_SECONDS",
    "GRAPH_TOOLS",
    "GRAPH_TRIGGER_GAUGE_VERSION",
    "GraphTriggersOut",
    "SITTING_DATE",
    "TriggerGauge",
    "WALK_TOOLS",
    "build_graph_triggers_router",
    "gauge_e1",
    "gauge_e2",
    "gauge_e3",
    "gauge_e4",
    "gauge_e5",
]
