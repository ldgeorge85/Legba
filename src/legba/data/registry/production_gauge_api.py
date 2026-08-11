# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""S-1 — the expected-vs-actual production gauge read surface.

``GET /api/v1/v3/system/production-gauge`` — one row per producing loop in the
engine (analyst cadence, analyst output, source signal production, declared
backlog drain), worst-first, with the roll-up counts an operator can read at a
glance. It sits in the ``/system/*`` freshness family beside
``/system/staleness-debt``, ``/system/analyst-cadence`` and
``/system/source-firing``, and answers the question none of those three can:
not *is it running* but *did it produce what it promised*.

The judgment is not implemented here. :mod:`.production_gauge` owns the whole
expectation model and the ``production_deficit`` alert-trigger class reads the
SAME function, so the table and the page can never disagree — the
:mod:`.source_freshness` precedent ("one grading implementation, two
readers"). That also means this route needs no mirrored SQL and therefore no
drift guard: there is nothing to drift from.

Shaped for a table
------------------
The response is deliberately flat and pre-sorted rather than nested by class.
An operator scanning a terminal or a panel wants ``class | loop | state |
severity | ratio | actual`` in reading order with the worst thing on line one;
grouping by class would bury a critical source deficit under forty healthy
analysts. ``totals`` carries the same numbers already aggregated so a tile can
render without walking the list.

Honesty contract
----------------
``ungauged`` is a first-class state, never folded into "ok". A loop with no
honest expectation — a paused descriptor, an on-demand analyst with no cron, a
side-effect sweep that has never written a row, a source with too little
history to have a baseline — reports ``state="ungauged"`` with the reason
named in ``quiet_reason``. The totals publish ``gauged`` and ``ungauged``
separately for exactly that reason: an engine where 200 of 268 loops cannot be
gauged is a different engine from one where 261 of 268 are producing, and a
single "healthy" percentage would hide the difference.

``ratio`` is ``null`` precisely when the loop is ungauged, so no reader can
mistake "no expectation" for a measured 0.0.

Degradation follows the family rule: any read failure logs at INFO and returns
an honest empty payload at HTTP 200 — a polling panel must never be handed a
500 to hammer. An empty gauge is visibly empty (``loops: []``,
``gauged: 0``), which reads as "we could not measure", not as "all clear".
"""
from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from . import production_gauge
from .api import RegistryAPIDeps, require_bearer

logger = logging.getLogger(__name__)

_ROUTE = "/system/production-gauge"

#: Hard cap on rows returned — the whole fleet is ~270 loops today; the bound
#: is defensive against a substrate that has grown an order of magnitude while
#: nobody updated the panel.
_MAX_LIMIT = 2_000


# ---------------------------------------------------------------------------
# Wire shapes
# ---------------------------------------------------------------------------


class ProductionGaugeRow(BaseModel):
    """One producing loop's expected-vs-actual verdict."""

    #: ``analyst_cadence`` | ``analyst_production`` | ``source_production`` |
    #: ``backlog_drain``.
    loop_class: str
    #: The analyst id / source descriptor id / declared backlog id.
    loop_id: str
    label: str
    #: ``ok`` | ``deficit`` | ``ungauged``. Never blended — see the module
    #: docstring's honesty contract.
    state: str
    #: Only meaningful when ``state == "deficit"``.
    severity: str = "info"
    #: Observed absence over the bar that absence had to clear. ``null``
    #: exactly when ``state == "ungauged"``.
    ratio: Optional[float] = None
    #: Plain-language statement of what this loop promised, and where the
    #: promise came from (its own cron / its own trailing rate).
    expected: str = ""
    #: Plain-language statement of what it actually did.
    actual: str = ""
    #: Why no expectation exists, when ``state == "ungauged"``.
    quiet_reason: Optional[str] = None
    #: Last run that wrote something / last signal / last successful fire,
    #: depending on the class. ``null`` when the loop has never produced.
    last_production_at: Optional[datetime] = None
    #: Would this row page through the alert plane? (``deficit`` at or above
    #: ``production_gauge.ALERT_MIN_SEVERITY``.) Published so the table and
    #: the operator's phone are visibly the same instrument.
    pages: bool = False
    #: The numbers the verdict was computed from — cron, interval, bar,
    #: observed gap, counts. Shape varies by class, deliberately: a cadence
    #: deficit and a source drought are not measured in the same units and
    #: flattening them would lose the units.
    evidence: dict[str, Any] = Field(default_factory=dict)


class ProductionGaugeTotals(BaseModel):
    """The one-glance roll-up."""

    loops: int = 0
    gauged: int = 0
    ok: int = 0
    deficit: int = 0
    ungauged: int = 0
    #: Deficits that clear the alert-plane floor.
    paging: int = 0
    by_severity: dict[str, int] = Field(default_factory=dict)
    #: Per loop_class: gauged / ok / deficit / ungauged.
    by_class: dict[str, dict[str, int]] = Field(default_factory=dict)


class ProductionGaugeOut(BaseModel):
    """The whole-engine read."""

    generated_at: Optional[datetime] = None
    #: Trailing history depth every baseline was computed over.
    window_days: int = 0
    #: The severity at or above which a deficit pages.
    alert_min_severity: str = production_gauge.ALERT_MIN_SEVERITY
    totals: ProductionGaugeTotals = Field(default_factory=ProductionGaugeTotals)
    loops: list[ProductionGaugeRow] = Field(default_factory=list)
    #: False when the gauge could not read the substrate at all — the honest
    #: "we measured nothing" flag, so an empty table is never mistaken for a
    #: clean one.
    measured: bool = False


def _row(gauge: Any) -> ProductionGaugeRow:
    return ProductionGaugeRow(
        loop_class=gauge.loop_class,
        loop_id=gauge.loop_id,
        label=gauge.label,
        state=gauge.state,
        severity=gauge.severity,
        ratio=gauge.ratio,
        expected=gauge.expected,
        actual=gauge.actual,
        quiet_reason=gauge.quiet_reason,
        last_production_at=gauge.last_production_at,
        pages=gauge.pages,
        evidence=gauge.evidence,
    )


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def build_production_gauge_router(deps: RegistryAPIDeps) -> APIRouter:
    router = APIRouter(tags=["system"])

    def _get_deps(request: Request) -> RegistryAPIDeps:
        return getattr(request.app.state, "registry_deps", deps)

    @router.get(_ROUTE, response_model=ProductionGaugeOut)
    async def system_production_gauge(
        loop_class: str | None = Query(
            default=None,
            description=(
                "Filter to one loop class: analyst_cadence | "
                "analyst_production | source_production | backlog_drain."
            ),
        ),
        state: str | None = Query(
            default=None,
            description="Filter to ok | deficit | ungauged.",
        ),
        deficits_only: bool = Query(
            default=False,
            description=(
                "Shorthand for state=deficit. The one-line answer to 'is "
                "anything not producing right now'."
            ),
        ),
        paging_only: bool = Query(
            default=False,
            description=(
                "Only deficits that clear the alert-plane floor — exactly the "
                "set the production_deficit trigger class would page on."
            ),
        ),
        window_days: int | None = Query(
            default=None,
            ge=1,
            le=365,
            description=(
                "Override the trailing baseline depth. Unset uses the gauge's "
                "own default; widening it makes long-dead producers visible "
                "again, narrowing it makes the baselines twitchier."
            ),
        ),
        limit: int = Query(default=500, ge=1, le=_MAX_LIMIT),
        _principal: str = Depends(require_bearer),
        deps_: RegistryAPIDeps = Depends(_get_deps),
    ) -> ProductionGaugeOut:
        """Every producing loop, worst-first, with its own expectation stated.

        ``totals`` is computed over the FULL gauge read, before any filter, so
        a filtered request still reports the true engine-wide picture — a
        ``?deficits_only=true`` call that returned ``deficit: 3`` alongside
        ``loops: 3`` would be lying about the denominator.
        """
        cfg = production_gauge.GaugeConfig()
        if window_days is not None:
            cfg = replace(cfg, window_days=window_days)
        try:
            async with deps_.descriptor_registry.pg.acquire() as conn:
                report = await production_gauge.read_gauge(conn, cfg=cfg)
        except Exception as exc:  # noqa: BLE001 — a panel must not get a 500
            logger.info("v3.system.production_gauge.unavailable err=%s", exc)
            return ProductionGaugeOut()

        totals = report.totals()
        rows = report.loops
        if paging_only:
            rows = [g for g in rows if g.pages]
        elif deficits_only:
            rows = [g for g in rows if g.state == "deficit"]
        if loop_class:
            rows = [g for g in rows if g.loop_class == loop_class]
        if state:
            rows = [g for g in rows if g.state == state]

        return ProductionGaugeOut(
            generated_at=report.generated_at,
            window_days=report.window_days,
            totals=ProductionGaugeTotals(**totals),
            loops=[_row(g) for g in rows[:limit]],
            measured=True,
        )

    return router


__all__ = [
    "ProductionGaugeOut",
    "ProductionGaugeRow",
    "ProductionGaugeTotals",
    "build_production_gauge_router",
]
