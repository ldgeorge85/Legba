# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Watchlist v2 CRUD — ``/api/v1/v3/watchlist`` (P5-6).

The operator's standing-watch management surface: create / list / update /
soft-delete rows in the ``watchlist`` table (migration 0105). The watches are
SERVER-side — unlike the Alert Center's localStorage subscriptions — because
the ``watchlist_hit`` trigger class inside ``alert_trigger_scan`` must be able
to evaluate them on its own cadence and page through the shared P1-1
dispatcher whether or not any UI is open.

The first WRITE surface in the v3 route family. Conventions mirror the rest
of the registry HTTP plane:

  * bearer-gated via :func:`~legba.data.registry.api.require_bearer`;
  * validation errors are 422 with a stated reason (never a silent coercion);
  * DELETE is SOFT — ``active=false`` — so the watch's watermark history
    survives and re-activation never re-pages already-seen hits;
  * ``kind`` is immutable after create (a pattern update revalidates against
    the row's kind); to change kind, delete + recreate;
  * ``created_by`` comes from the request body (default ``operator``), NOT
    from the bearer principal — the principal is the raw token value when a
    token is configured, and a secret must never be persisted into a data
    table.

``hits_7d`` on the list read is the count of ``kind='alert'`` rows naming the
watch (``data.data.watch_id``) over the last 7 days — per-watch rollup rows
count as ONE row each (their honest ``suppressed_count`` lives in the row).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Literal, Mapping, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from .api import RegistryAPIDeps, require_bearer

logger = logging.getLogger(__name__)

WATCH_KINDS = ("entity", "text", "geo")
SEVERITIES = ("info", "low", "medium", "high", "critical")

_MAX_LABEL = 200
_MAX_NAME = 300
_MAX_QUERY = 300
_MAX_COUNTRIES = 50
_MAX_LIST = 500


# ---------------------------------------------------------------------------
# Pattern validation (pure — unit-testable without the app)
# ---------------------------------------------------------------------------


def validate_pattern(kind: str, pattern: Mapping[str, Any]) -> dict[str, Any]:
    """Validate + normalize one watch pattern for its kind.

    Returns the normalized pattern dict; raises ``ValueError`` with a stated
    reason on any violation (the route maps that to a 422). Closed key sets
    per kind — junk keys are rejected, not ignored, so a typo ("countires")
    fails loudly at create time instead of silently matching nothing forever.
    """
    if not isinstance(pattern, Mapping):
        raise ValueError("pattern must be an object")

    if kind == "entity":
        allowed = {"name", "entity_id"}
        extra = set(pattern) - allowed
        if extra:
            raise ValueError(
                f"entity pattern allows keys {sorted(allowed)}; got extras "
                f"{sorted(extra)}"
            )
        out: dict[str, Any] = {}
        name = pattern.get("name")
        if name is not None:
            if not isinstance(name, str) or not name.strip():
                raise ValueError("entity pattern 'name' must be a non-empty string")
            if len(name.strip()) > _MAX_NAME:
                raise ValueError(f"entity 'name' longer than {_MAX_NAME} chars")
            out["name"] = name.strip()
        eid = pattern.get("entity_id")
        if eid is not None:
            try:
                out["entity_id"] = str(UUID(str(eid)))
            except (ValueError, TypeError):
                raise ValueError("entity pattern 'entity_id' must be a UUID")
        if not out:
            raise ValueError(
                "entity pattern requires 'name' and/or 'entity_id'"
            )
        return out

    if kind == "text":
        allowed = {"query"}
        extra = set(pattern) - allowed
        if extra:
            raise ValueError(
                f"text pattern allows keys {sorted(allowed)}; got extras "
                f"{sorted(extra)}"
            )
        query = pattern.get("query")
        if not isinstance(query, str) or len(query.strip()) < 2:
            raise ValueError(
                "text pattern 'query' must be a string of >= 2 characters"
            )
        if len(query.strip()) > _MAX_QUERY:
            raise ValueError(f"text 'query' longer than {_MAX_QUERY} chars")
        return {"query": query.strip()}

    if kind == "geo":
        has_countries = "countries" in pattern
        has_point = any(k in pattern for k in ("lat", "lon", "radius_km"))
        if has_countries and has_point:
            raise ValueError(
                "geo pattern is EITHER {'countries': [...]} OR "
                "{'lat','lon','radius_km'} — not both"
            )
        if has_countries:
            extra = set(pattern) - {"countries"}
            if extra:
                raise ValueError(f"geo country pattern got extras {sorted(extra)}")
            raw = pattern.get("countries")
            if not isinstance(raw, list) or not raw:
                raise ValueError("geo 'countries' must be a non-empty list")
            if len(raw) > _MAX_COUNTRIES:
                raise ValueError(f"geo 'countries' capped at {_MAX_COUNTRIES}")
            codes: list[str] = []
            for c in raw:
                if (
                    not isinstance(c, str)
                    or len(c.strip()) != 2
                    or not c.strip().isalpha()
                ):
                    raise ValueError(
                        f"geo country codes must be 2-letter ISO2; got {c!r}"
                    )
                code = c.strip().upper()
                if code not in codes:
                    codes.append(code)
            return {"countries": codes}
        if has_point:
            extra = set(pattern) - {"lat", "lon", "radius_km"}
            if extra:
                raise ValueError(f"geo point pattern got extras {sorted(extra)}")
            try:
                lat = float(pattern["lat"])
                lon = float(pattern["lon"])
                radius_km = float(pattern["radius_km"])
            except (KeyError, TypeError, ValueError):
                raise ValueError(
                    "geo point pattern requires numeric 'lat', 'lon' and "
                    "'radius_km'"
                )
            if not -90.0 <= lat <= 90.0:
                raise ValueError("geo 'lat' must be within [-90, 90]")
            if not -180.0 <= lon <= 180.0:
                raise ValueError("geo 'lon' must be within [-180, 180]")
            if not 0.0 < radius_km <= 1000.0:
                raise ValueError("geo 'radius_km' must be in (0, 1000]")
            return {"lat": lat, "lon": lon, "radius_km": radius_km}
        raise ValueError(
            "geo pattern requires {'countries': [...]} or "
            "{'lat','lon','radius_km'}"
        )

    raise ValueError(f"unknown watch kind {kind!r} (one of {list(WATCH_KINDS)})")


# ---------------------------------------------------------------------------
# Wire models
# ---------------------------------------------------------------------------


class WatchCreate(BaseModel):
    kind: Literal["entity", "text", "geo"]
    pattern: dict[str, Any]
    label: str = Field(min_length=1, max_length=_MAX_LABEL)
    min_severity: Literal["info", "low", "medium", "high", "critical"] | None = None
    created_by: str = Field(default="operator", min_length=1, max_length=128)


class WatchUpdate(BaseModel):
    """Partial update. ``min_severity`` distinguishes omitted (keep) from an
    explicit ``null`` (clear the floor) via ``model_fields_set``."""

    label: Optional[str] = Field(default=None, min_length=1, max_length=_MAX_LABEL)
    pattern: Optional[dict[str, Any]] = None
    min_severity: Literal["info", "low", "medium", "high", "critical"] | None = None
    active: Optional[bool] = None


class WatchOut(BaseModel):
    id: str
    kind: str
    pattern: dict[str, Any]
    label: str
    min_severity: str | None = None
    created_by: str
    active: bool
    created_at: datetime
    updated_at: datetime
    hits_7d: int = 0


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


_LIST_SQL = """
    SELECT w.id::text AS id, w.kind, w.pattern, w.label, w.min_severity,
           w.created_by, w.active, w.created_at, w.updated_at,
           (SELECT count(*) FROM analyst_outputs a
             WHERE a.kind = 'alert'
               AND a.produced_at > now() - interval '7 days'
               AND a.data->'data'->>'watch_id' = w.id::text) AS hits_7d
      FROM watchlist w
     WHERE ($1::bool OR w.active)
     ORDER BY w.created_at DESC, w.id
     LIMIT $2
"""

_GET_SQL = """
    SELECT w.id::text AS id, w.kind, w.pattern, w.label, w.min_severity,
           w.created_by, w.active, w.created_at, w.updated_at,
           (SELECT count(*) FROM analyst_outputs a
             WHERE a.kind = 'alert'
               AND a.produced_at > now() - interval '7 days'
               AND a.data->'data'->>'watch_id' = w.id::text) AS hits_7d
      FROM watchlist w
     WHERE w.id = $1
"""


def _watch_out(row: Any) -> WatchOut:
    pattern = row["pattern"]
    if isinstance(pattern, str):
        try:
            pattern = json.loads(pattern)
        except (ValueError, TypeError):
            pattern = {}
    return WatchOut(
        id=str(row["id"]),
        kind=str(row["kind"]),
        pattern=pattern if isinstance(pattern, dict) else {},
        label=str(row["label"]),
        min_severity=row["min_severity"],
        created_by=str(row["created_by"]),
        active=bool(row["active"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        hits_7d=int(row.get("hits_7d") or 0) if hasattr(row, "get") else int(row["hits_7d"] or 0),
    )


def _coerce_watch_uuid(watch_id: str) -> UUID:
    try:
        return UUID(str(watch_id))
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"watch id must be a UUID; got {watch_id!r}",
        )


def build_watchlist_router(deps: RegistryAPIDeps) -> APIRouter:
    """Construct the watchlist CRUD router (mount under ``/api/v1/v3``)."""
    router = APIRouter(tags=["watchlist"])

    @router.get("/watchlist", response_model=list[WatchOut])
    async def list_watches(
        include_inactive: bool = Query(default=False),
        principal: str = Depends(require_bearer),
    ) -> list[WatchOut]:
        """Active watches (newest first) + each watch's 7-day alert-row count.
        ``include_inactive=true`` also returns soft-deleted rows."""
        async with deps.descriptor_registry.pg.acquire() as conn:
            rows = await conn.fetch(_LIST_SQL, include_inactive, _MAX_LIST)
        return [_watch_out(r) for r in rows]

    @router.post(
        "/watchlist",
        response_model=WatchOut,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_watch(
        body: WatchCreate,
        principal: str = Depends(require_bearer),
    ) -> WatchOut:
        try:
            pattern = validate_pattern(body.kind, body.pattern)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc
        async with deps.descriptor_registry.pg.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO watchlist (kind, pattern, label, min_severity,
                                       created_by)
                VALUES ($1, $2::jsonb, $3, $4, $5)
                RETURNING id::text AS id, kind, pattern, label, min_severity,
                          created_by, active, created_at, updated_at,
                          0::bigint AS hits_7d
                """,
                body.kind,
                json.dumps(pattern),
                body.label.strip(),
                body.min_severity,
                body.created_by.strip(),
            )
        logger.info(
            "watchlist.created id=%s kind=%s label=%r",
            row["id"],
            body.kind,
            body.label,
        )
        return _watch_out(row)

    @router.put("/watchlist/{watch_id}", response_model=WatchOut)
    async def update_watch(
        watch_id: str,
        body: WatchUpdate,
        principal: str = Depends(require_bearer),
    ) -> WatchOut:
        wid = _coerce_watch_uuid(watch_id)
        async with deps.descriptor_registry.pg.acquire() as conn:
            row = await conn.fetchrow(_GET_SQL, wid)
            if row is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"watch {watch_id} not found",
                )
            sets: list[str] = ["updated_at = now()"]
            args: list[Any] = []
            if body.label is not None:
                args.append(body.label.strip())
                sets.append(f"label = ${len(args)}")
            if body.pattern is not None:
                # kind is immutable — revalidate against the ROW's kind.
                try:
                    pattern = validate_pattern(str(row["kind"]), body.pattern)
                except ValueError as exc:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=str(exc),
                    ) from exc
                args.append(json.dumps(pattern))
                sets.append(f"pattern = ${len(args)}::jsonb")
            if "min_severity" in body.model_fields_set:
                args.append(body.min_severity)
                sets.append(f"min_severity = ${len(args)}")
            if body.active is not None:
                args.append(body.active)
                sets.append(f"active = ${len(args)}")
            args.append(wid)
            updated = await conn.fetchrow(
                f"""
                UPDATE watchlist SET {', '.join(sets)}
                 WHERE id = ${len(args)}
                RETURNING id::text AS id, kind, pattern, label, min_severity,
                          created_by, active, created_at, updated_at,
                          0::bigint AS hits_7d
                """,
                *args,
            )
        return _watch_out(updated)

    @router.delete("/watchlist/{watch_id}", response_model=WatchOut)
    async def delete_watch(
        watch_id: str,
        principal: str = Depends(require_bearer),
    ) -> WatchOut:
        """SOFT delete: flips ``active=false`` (the row + its no-refire
        watermark history survive). Idempotent — deleting an already-inactive
        watch returns it unchanged."""
        wid = _coerce_watch_uuid(watch_id)
        async with deps.descriptor_registry.pg.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE watchlist
                   SET active = FALSE, updated_at = now()
                 WHERE id = $1
                RETURNING id::text AS id, kind, pattern, label, min_severity,
                          created_by, active, created_at, updated_at,
                          0::bigint AS hits_7d
                """,
                wid,
            )
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"watch {watch_id} not found",
            )
        logger.info("watchlist.soft_deleted id=%s", row["id"])
        return _watch_out(row)

    return router
