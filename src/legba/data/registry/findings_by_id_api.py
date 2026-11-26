# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``GET /findings/{finding_id}`` — ONE finding at full weight, by id.

The list route (``GET /findings``) answers at a light weight by default
(``fields=summary``); the full row — ``data``, ``body``, ``derived_from`` and
both verification folds — is read here, for the one record a reader opened,
or on the list by asking ``fields=full`` by name. Before this route a client
that held a finding's id (the phone's report view, a deep link) had to re-find
the row through the list's filters.

Same SQL and same hydration as the list's full weight
(``substrate_reads_folds.full_row_sql`` + ``substrate_reads_api.
_hydrate_finding``), so a row read by id is byte-identical to the same row on a
``fields=full`` page. Bearer-gated; role ``read``
(``route_policy.ROUTE_MIN_ROLES``). Registry-slim: fastapi + the leaf folds;
the hydrator is handed in by the router that mounts this one (no import cycle).
404 when the id names no finding (or names a row of another kind); 400 on a
malformed id.
"""
from __future__ import annotations

from typing import Any, Callable
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status

from .api import RegistryAPIDeps, require_bearer
from .read_latency import fast_json_response
from .substrate_reads_folds import full_row_sql, parse_full_row


def build_finding_by_id_router(
    deps: RegistryAPIDeps, *, hydrate: Callable[[Any], Any],
) -> APIRouter:
    router = APIRouter()

    @router.get("/findings/{finding_id}", response_model=None)
    async def get_finding(
        finding_id: str,
        principal: str = Depends(require_bearer),
    ) -> Response:
        try:
            fid = UUID(finding_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="finding_id must be a UUID",
            ) from exc
        sql = full_row_sql(
            "f.kind = 'finding' AND f.id = $1", "LIMIT 1",
            "ORDER BY f.produced_at DESC, f.id DESC",
        )
        async with deps.descriptor_registry.pg.acquire() as conn:
            row = await conn.fetchrow(sql, fid)
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="no such finding",
            )
        return fast_json_response(hydrate(parse_full_row(row)))

    return router
