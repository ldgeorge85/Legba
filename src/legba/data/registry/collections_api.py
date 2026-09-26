# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``GET /api/v1/v3/collections/coverage`` — the ERA COVERAGE MAP for one desk.

What the platform HOLDS of the past for this desk, series by series: the
valid-time spans a curated collection's manifest DECLARES the provider has,
the spans the ``observations`` table actually carries, and the holes between
them. A cited historical number is only as checkable as the answer to "and
what else was there", and before this route that answer existed nowhere — the
load ledger says how many rows were written, which is a different question
from which YEARS are on record.

The comparison itself lives in :mod:`.collections_coverage`, shared with the
``history_gap`` typed absence on ``/v3/absence``: one reader, two surfaces, so
the map and the absence can never disagree about the same silence.

Registry-slim: stdlib + fastapi + pydantic + ``.api`` + ``.collections_coverage``
(itself stdlib + pydantic). Nothing from ``legba.data.analysts`` or
``legba.runtime`` — this module ships in the slim registry image and
``tests/data_pkg/test_collections_coverage_api.py`` pins it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from .api import RegistryAPIDeps, require_bearer
from .collections_coverage import (
    COLLECTIONS_COVERAGE_VERSION,
    CoverageOut,
    read_coverage,
)

logger = logging.getLogger(__name__)

_ROUTE = "/collections/coverage"

__all__ = [
    "COLLECTIONS_COVERAGE_VERSION",
    "CoverageOut",
    "build_collections_router",
]


def build_collections_router(deps: RegistryAPIDeps) -> APIRouter:
    """Build the read-only router (mounted at ``/api/v1/v3``)."""
    router = APIRouter(tags=["v3"])

    def _get_deps(request: Request) -> RegistryAPIDeps:
        return getattr(request.app.state, "registry_deps", deps)

    @router.get(_ROUTE, response_model=CoverageOut)
    async def collections_coverage(
        request: Request,
        scope: str = Query(
            ...,
            description=(
                "The desk (target_id) the coverage map is bounded to. A desk "
                "no loaded collection names is answered with an empty map and "
                "a sentence naming the desks the holdings do cover — never "
                "with an error and never with a fabricated row."
            ),
        ),
        principal: str = Depends(require_bearer),
    ) -> CoverageOut:
        """The era coverage map for one desk — declared spans, held spans, holes.

        ``collections: []`` is a real answer: no loaded holding names this
        desk. ``not_held`` then carries the route's own words for it, in the
        same posture ``/v3/absence`` takes with ``not_measured`` — an absence
        of a holding is not an empty history.
        """
        desk = (scope or "").strip()
        if not desk:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "query parameter 'scope' is required and must name a desk "
                    "(target_id): GET /v3/collections/coverage?scope=<target_id>"
                ),
            )
        d = _get_deps(request)
        async with d.descriptor_registry.pg.acquire() as conn:
            return await read_coverage(
                conn, scope=desk, now=datetime.now(timezone.utc)
            )

    return router
