# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``GET /v3/system/search-health?days=7`` — how often the search plane ran
degraded, per provider and per upstream engine (X1/B5, SW-3).

The judge has a health read (``/v3/system/judge-stats/health``); the search
plane that decides every ``searched_found_nothing`` absence had none, and the
fact that EVERY SearXNG search was losing an engine (reuters, on every query in
the 10-05 → 10-08 runtime log) was visible only by grepping a container log.

WHAT IT READS. The external audit's PRIMARY grade rows inside the window — one
row per search that decided a claim — judged by the ONE rule
(:func:`.search_health.read_search_health`) the absence route and the
watchdog's ``search_degraded`` page also read:

  * ``providers[*]`` — searches, the share the rule calls degraded, and the
    same share over NOT_FOUND rows (the ones that would otherwise read "we
    looked and found nothing");
  * ``engines[*]`` — each upstream engine's error share over the rows that
    RECORDED engines (``external_grades.search_health``, migration 0235), with
    the provider's own error reasons counted. Rows written before the health
    block existed are counted in ``rows_without_engine_record`` and are never
    a denominator for an engine.

HONESTY. A share with no denominator is ``null``, never ``0``. A failed read is
``measured: false`` with the reason, at HTTP 200 (a polling surface never gets a
500). ``as_of`` is the newest grade read, not the route's clock.

Registry-slim: stdlib + fastapi + pydantic + ``.api`` + ``.search_health``.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from .api import RegistryAPIDeps, require_bearer
from .search_health import (
    DEGRADED_MIN_UNRESPONSIVE_ENV,
    SEARCH_HEALTH_WINDOW_SQL,
    degraded_min_unresponsive,
    summarize,
)

logger = logging.getLogger(__name__)

SEARCH_HEALTH_ROUTE_VERSION = "2026-10/b5"
_ROUTE = "/system/search-health"
DEFAULT_WINDOW_DAYS = 7
MAX_WINDOW_DAYS = 30

_BASIS = (
    "the external audit's primary grade rows in the window (one per search that "
    "decided a claim), judged by the search-health rule: degraded when "
    "unresponsive engines reach the threshold, or zero results came back with "
    "an engine error; a row with no engine record is read by the provider's "
    "own degraded flag"
)


class SearchProviderHealth(BaseModel):
    provider: str
    searches: int
    degraded: int
    degraded_share: Optional[float] = None
    not_found: int
    not_found_degraded: int
    not_found_degraded_share: Optional[float] = None


class SearchEngineHealth(BaseModel):
    engine: str
    #: Rows that recorded engines — the denominator of ``error_share``.
    searches: int
    errored: int
    answered: int
    error_share: Optional[float] = None
    #: The provider's own error reasons, counted ("HTTP error", "timeout", …).
    errors: dict[str, int] = Field(default_factory=dict)


class SearchHealthOut(BaseModel):
    version: str = SEARCH_HEALTH_ROUTE_VERSION
    measured: bool
    not_measured_reason: Optional[str] = None
    window_days: int
    read_at: str
    as_of: Optional[str] = None
    threshold: int
    threshold_env: str = DEGRADED_MIN_UNRESPONSIVE_ENV
    basis: str = _BASIS
    rows_with_engine_record: int = 0
    rows_without_engine_record: int = 0
    providers: list[SearchProviderHealth] = Field(default_factory=list)
    engines: list[SearchEngineHealth] = Field(default_factory=list)


async def read_search_health_window(
    conn, *, days: int, now: Optional[datetime] = None,
) -> SearchHealthOut:
    """The route's whole answer off an open connection (also the test seam)."""
    now = now or datetime.now(timezone.utc)
    threshold = degraded_min_unresponsive()
    try:
        rows = await conn.fetch(SEARCH_HEALTH_WINDOW_SQL, now - timedelta(days=days))
    except Exception as exc:  # noqa: BLE001 — a polling surface never 500s
        logger.info("v3.system.search_health.unavailable err=%s", exc)
        return SearchHealthOut(
            measured=False,
            not_measured_reason=(
                f"external_grades could not be read ({type(exc).__name__})"
            ),
            window_days=days, read_at=now.isoformat(), threshold=threshold,
        )
    summary = summarize([dict(r) for r in rows], threshold=threshold)
    return SearchHealthOut(
        measured=True,
        window_days=days,
        read_at=now.isoformat(),
        as_of=summary["as_of"],
        threshold=summary["threshold"],
        rows_with_engine_record=summary["rows_with_engine_record"],
        rows_without_engine_record=summary["rows_without_engine_record"],
        providers=[SearchProviderHealth(**p) for p in summary["providers"]],
        engines=[SearchEngineHealth(**e) for e in summary["engines"]],
    )


def build_search_health_router(deps: RegistryAPIDeps) -> APIRouter:
    router = APIRouter(tags=["system"])

    def _get_deps(request: Request) -> RegistryAPIDeps:
        return getattr(request.app.state, "registry_deps", deps)

    @router.get(_ROUTE, response_model=SearchHealthOut)
    async def system_search_health(
        days: int = Query(
            default=DEFAULT_WINDOW_DAYS, ge=1, le=MAX_WINDOW_DAYS,
            description="Trailing window in days.",
        ),
        _principal: str = Depends(require_bearer),
        deps_: RegistryAPIDeps = Depends(_get_deps),
    ) -> SearchHealthOut:
        try:
            async with deps_.descriptor_registry.pg.acquire() as conn:
                return await read_search_health_window(conn, days=days)
        except Exception as exc:  # noqa: BLE001 — a polling surface never 500s
            logger.info("v3.system.search_health.unavailable err=%s", exc)
            return SearchHealthOut(
                measured=False,
                not_measured_reason=f"no connection ({type(exc).__name__})",
                window_days=days,
                read_at=datetime.now(timezone.utc).isoformat(),
                threshold=degraded_min_unresponsive(),
            )

    return router


__all__ = [
    "DEFAULT_WINDOW_DAYS",
    "MAX_WINDOW_DAYS",
    "SEARCH_HEALTH_ROUTE_VERSION",
    "SearchEngineHealth",
    "SearchHealthOut",
    "SearchProviderHealth",
    "build_search_health_router",
    "read_search_health_window",
]
