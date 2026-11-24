# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``GET /v3/counts`` — how many signals and findings landed in a window.

W11-F271. The World KPI strip's "Signals 24h" and "Findings 24h" cards read
the newest-500 page of ``/signals`` and ``/findings`` with no time bound and
printed its length, so on any real day they said "500+" and the label was not
true. No existing route carries a 24-hour count of either: ``/signals`` and
``/findings`` page rows (a 24 h findings page is ~700 summary rows, a signals
page ~4,400 full rows), ``/v3/system/source-firing`` is a per-source list
that counts every row including re-poll aliases, ``/v3/ingestion/intake``
counts fetch requests, and ``/v3/since``'s counts are verified findings past
a client cursor. So this is two ``count(*)`` statements and nothing else.

Each count mirrors its list route's own filter, so the card and the list
agree on what a row is:

  * ``signals`` — canonical rows (``CANONICAL_ONLY_SQL``, the ``/signals``
    clause) fetched in the window: ``idx_signals_retention_fetched_at``,
    ~39 ms for ~4,400 rows live (EXPLAIN ANALYZE, 2026-11-14);
  * ``findings`` — ``kind = 'finding'`` produced in the window (the
    ``/findings`` base clause): ``idx_analyst_outputs_kind``, ~6 ms for ~720
    rows live.

An answer is reused for :data:`COUNTS_TTL_S` per window (the strip polls once
a minute; many open strips cost one count). Registry-slim: stdlib + fastapi +
pydantic + ``.api`` + the stdlib-only ``signal_slice_scope`` constant.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from ..signal_slice_scope import CANONICAL_ONLY_SQL
from .api import RegistryAPIDeps, require_bearer

logger = logging.getLogger(__name__)

_ROUTE = "/counts"
MAX_WINDOW_HOURS = 168
#: Seconds a computed answer is reused, per window.
COUNTS_TTL_S = 30.0

_SIGNALS_SQL = (
    "SELECT count(*) FROM public.signals "
    f"WHERE fetched_at >= $1 AND {CANONICAL_ONLY_SQL}"
)
_FINDINGS_SQL = (
    "SELECT count(*) FROM public.analyst_outputs "
    "WHERE kind = 'finding' AND produced_at >= $1"
)


class WindowCountsOut(BaseModel):
    window_hours: int
    since: datetime
    signals: int
    findings: int


_CACHE: dict[int, tuple[float, WindowCountsOut]] = {}


def build_window_counts_router(deps: RegistryAPIDeps) -> APIRouter:
    """Read-only router (mounted at ``/api/v1/v3``)."""
    router = APIRouter(tags=["substrate"])

    def _get_deps(request: Request) -> RegistryAPIDeps:
        return getattr(request.app.state, "registry_deps", deps)

    @router.get(_ROUTE, response_model=WindowCountsOut)
    async def window_counts(
        request: Request,
        window_hours: int = Query(default=24, ge=1, le=MAX_WINDOW_HOURS),
        principal: str = Depends(require_bearer),
    ) -> WindowCountsOut:
        hit = _CACHE.get(window_hours)
        now_mono = time.monotonic()
        if hit is not None and now_mono - hit[0] < COUNTS_TTL_S:
            return hit[1]
        since = datetime.now(timezone.utc) - timedelta(hours=window_hours)
        d = _get_deps(request)
        async with d.descriptor_registry.pg.acquire() as conn:
            signals = await conn.fetchval(_SIGNALS_SQL, since)
            findings = await conn.fetchval(_FINDINGS_SQL, since)
        out = WindowCountsOut(window_hours=window_hours, since=since,
                              signals=int(signals or 0), findings=int(findings or 0))
        _CACHE[window_hours] = (now_mono, out)
        return out

    return router


__all__ = ["COUNTS_TTL_S", "WindowCountsOut", "build_window_counts_router"]
