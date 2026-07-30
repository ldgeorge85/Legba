# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Source-credibility CRUD endpoints (P-8 Source Credibility panel).

Five HTTP endpoints over the ``source_credibility`` table (migration 0014
— see ``src/legba/data/migrations/0014_source_credibility.sql``):

  * ``GET    /api/v1/source_credibility``           — list rows, optional
                                                       ``host`` substring
                                                       filter.
  * ``GET    /api/v1/source_credibility/{host}``    — single row.
  * ``PUT    /api/v1/source_credibility/{host}``    — upsert (insert if
                                                       absent, update if
                                                       present); body
                                                       ``{score, score_rationale,
                                                       scored_by}``.
  * ``DELETE /api/v1/source_credibility/{host}``    — remove.
  * ``POST   /api/v1/source_credibility/bulk``      — CSV upload of
                                                       ``host,score,score_rationale,scored_by``
                                                       rows; per-row errors
                                                       are reported, the
                                                       whole upload doesn't
                                                       fail on one bad row.

Mount on the registry app via:

    from legba.data.registry.source_credibility_api import (
        build_source_credibility_router,
    )
    app.include_router(
        build_source_credibility_router(deps), prefix="/api/v1",
    )

(The canonical ``src/legba/data/registry/server.py`` is deliberately not
modified by this commit — the registry-launcher wiring + deployment
configuration update lands as a follow-up.  Tests construct the app
themselves via :func:`build_source_credibility_router` and a real
:class:`RegistryAPIDeps`.)

Design notes
------------

  * Score validation: the migration declares ``CHECK (score BETWEEN 0.0
    AND 1.0)``.  We enforce the same range at the API edge so bulk
    uploads can report a row-level error string instead of letting the
    DB error abort the request.

  * ``scored_at`` (the table column ``last_updated``) is stamped
    server-side on every PUT / upsert.  The wire format spells the field
    ``scored_at`` per the P-8 panel contract; the underlying column name
    is preserved for migration stability.

  * The PUT body deliberately requires ``scored_by`` — no defaulting to
    ``anonymous`` here, because operator-attributed scores are the whole
    point of the panel (the system seed sets ``scored_by='system.seed'``
    at migration time; UI overrides set the operator's identity).

  * Bulk upload: ``Content-Type: text/csv``.  We accept either the four
    canonical column names as a header row or positional ordering when
    the header is missing.  Rows missing a required cell, with a malformed
    score, or with a score outside ``[0.0, 1.0]``, are reported in the
    ``errors`` list with the row number and the rejection reason; the
    rest of the file still upserts.

Deprecation window (C3)
-----------------------

The two READ endpoints (``GET /source_credibility`` and
``GET /source_credibility/{host}``) are superseded by the merged
source-quality ledger (``GET /api/v1/v3/source-quality``), which serves the
host score alongside the asserted Admiralty grade, the earned track record and
the computed freshness grade — keyed by SOURCE, with the host resolved the same
way the signal write path resolves it.  Both keep serving their original wire
shape until the sunset date, stamped ``Deprecation`` / ``Sunset`` / ``Link``.

The WRITE endpoints (PUT / DELETE / bulk) are **not** deprecated: the ledger is
a read surface and has no successor for them.  This module remains the only way
to author a host credibility score.
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from .api import RegistryAPIDeps, require_bearer, sunset_headers

logger = logging.getLogger(__name__)

#: C3 successor for the READ endpoints only — the merged ledger keys on source
#: descriptor id and resolves the host itself, so there is no per-host
#: successor path to point a single-host GET at.
SUCCESSOR_ROUTE = "/api/v1/v3/source-quality"


# ---------------------------------------------------------------------------
# Wire shapes
# ---------------------------------------------------------------------------


class SourceCredibilityRow(BaseModel):
    """JSON view of one ``source_credibility`` table row."""

    host: str
    score: float = Field(ge=0.0, le=1.0)
    score_rationale: str | None = None
    scored_at: datetime
    scored_by: str


class SourceCredibilityUpsert(BaseModel):
    """Body of PUT ``/source_credibility/{host}``."""

    score: float = Field(ge=0.0, le=1.0)
    score_rationale: str | None = None
    scored_by: str = Field(min_length=1, max_length=256)


class BulkRowError(BaseModel):
    row_number: int
    host: str | None = None
    error: str


class BulkUploadResult(BaseModel):
    inserted: int
    updated: int
    errors: list[BulkRowError] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_CANONICAL_COLUMNS = ("host", "score", "score_rationale", "scored_by")


def _row_out(row: Any) -> SourceCredibilityRow:
    return SourceCredibilityRow(
        host=row["source_host"],
        score=float(row["score"]),
        score_rationale=row["score_rationale"],
        scored_at=row["last_updated"],
        scored_by=row["scored_by"],
    )


def _get_deps(request: Request) -> RegistryAPIDeps:
    deps = getattr(request.app.state, "registry_deps", None)
    if deps is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "source_credibility api not configured "
                "(missing RegistryAPIDeps on app.state)"
            ),
        )
    return deps


async def _upsert_row(
    deps_: RegistryAPIDeps,
    *,
    host: str,
    score: float,
    score_rationale: str | None,
    scored_by: str,
    conn: Any | None = None,
) -> tuple[SourceCredibilityRow, bool]:
    """Insert-or-update one row.  Returns ``(row, inserted)``.

    ``inserted`` is ``True`` when the row didn't previously exist.
    Using the ``(xmax = 0)`` discriminator on the RETURNING clause is the
    canonical Postgres idiom for distinguishing INSERT vs. UPDATE on an
    ``INSERT ... ON CONFLICT DO UPDATE``: a row that was inserted in this
    statement has ``xmax = 0`` because no prior version was overwritten.
    """
    sql = """
        INSERT INTO source_credibility
            (source_host, score, score_rationale, scored_by, last_updated)
        VALUES ($1, $2, $3, $4, NOW())
        ON CONFLICT (source_host) DO UPDATE
        SET score = EXCLUDED.score,
            score_rationale = EXCLUDED.score_rationale,
            scored_by = EXCLUDED.scored_by,
            last_updated = NOW()
        RETURNING source_host, score, score_rationale, scored_by,
                  last_updated, (xmax = 0) AS inserted
    """
    args = (host, score, score_rationale, scored_by)
    if conn is not None:
        row = await conn.fetchrow(sql, *args)
    else:
        async with deps_.descriptor_registry.pg.acquire() as c:
            row = await c.fetchrow(sql, *args)
    if row is None:  # pragma: no cover — RETURNING always yields a row
        raise HTTPException(
            status_code=500,
            detail="source_credibility upsert returned no row",
        )
    return _row_out(row), bool(row["inserted"])


def _parse_csv_rows(blob: bytes) -> list[dict[str, str]]:
    """Parse a CSV blob into a list of dicts.

    Accepts either a header row (``host,score,score_rationale,scored_by``
    or any superset) OR a headerless file in the canonical column order.
    """
    try:
        text = blob.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"bulk CSV must be UTF-8 encoded: {exc}",
        ) from exc

    if not text.strip():
        return []

    sample = text[:4096]
    has_header = csv.Sniffer().has_header(sample) if sample.strip() else False

    if has_header:
        reader = csv.DictReader(io.StringIO(text))
        missing = [c for c in _CANONICAL_COLUMNS if c not in (reader.fieldnames or ())]
        if missing:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"bulk CSV header missing required columns {missing}; "
                    f"got fieldnames={reader.fieldnames!r}"
                ),
            )
        return [dict(r) for r in reader]
    # Headerless — positional.
    reader = csv.reader(io.StringIO(text))
    out: list[dict[str, str]] = []
    for raw in reader:
        if not raw or all(not cell.strip() for cell in raw):
            continue
        cells = list(raw) + [""] * (len(_CANONICAL_COLUMNS) - len(raw))
        out.append({col: cells[i] for i, col in enumerate(_CANONICAL_COLUMNS)})
    return out


def _validate_csv_row(idx: int, raw: dict[str, str]) -> tuple[
    str, float, str | None, str,
] | BulkRowError:
    """Validate one CSV row.  Returns parsed tuple or a BulkRowError."""
    host = (raw.get("host") or "").strip()
    score_raw = (raw.get("score") or "").strip()
    score_rationale = raw.get("score_rationale")
    if score_rationale is not None:
        score_rationale = score_rationale.strip() or None
    scored_by = (raw.get("scored_by") or "").strip()

    if not host:
        return BulkRowError(row_number=idx, host=None, error="host is required")
    if not scored_by:
        return BulkRowError(
            row_number=idx, host=host, error="scored_by is required",
        )
    if not score_raw:
        return BulkRowError(
            row_number=idx, host=host, error="score is required",
        )
    try:
        score = float(score_raw)
    except ValueError:
        return BulkRowError(
            row_number=idx,
            host=host,
            error=f"score is not a number: {score_raw!r}",
        )
    if not (0.0 <= score <= 1.0):
        return BulkRowError(
            row_number=idx,
            host=host,
            error=(
                f"score {score!r} is outside [0.0, 1.0] "
                f"(migration 0014 CHECK constraint)"
            ),
        )
    return host, score, score_rationale, scored_by


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_source_credibility_router(deps: RegistryAPIDeps) -> APIRouter:
    """Build the source_credibility router bound to ``deps``.

    Mount on a FastAPI app via ``app.include_router(router, prefix="/api/v1")``
    after wiring ``app.state.registry_deps = deps``.
    """
    router = APIRouter(tags=["source_credibility"])

    @router.get(
        "/source_credibility",
        response_model=list[SourceCredibilityRow],
        deprecated=True,
        dependencies=[Depends(sunset_headers(SUCCESSOR_ROUTE))],
    )
    async def list_source_credibility(
        host: str | None = Query(
            default=None,
            description=(
                "Optional substring match against the host column "
                "(case-insensitive). Empty / unset returns all rows."
            ),
        ),
        limit: int = Query(default=500, ge=1, le=5000),
        _principal: str = Depends(require_bearer),
        deps_: RegistryAPIDeps = Depends(_get_deps),
    ) -> list[SourceCredibilityRow]:
        async with deps_.descriptor_registry.pg.acquire() as conn:
            if host:
                rows = await conn.fetch(
                    """
                    SELECT source_host, score, score_rationale,
                           scored_by, last_updated
                      FROM source_credibility
                     WHERE source_host ILIKE $1
                     ORDER BY source_host
                     LIMIT $2
                    """,
                    f"%{host}%",
                    limit,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT source_host, score, score_rationale,
                           scored_by, last_updated
                      FROM source_credibility
                     ORDER BY source_host
                     LIMIT $1
                    """,
                    limit,
                )
        return [_row_out(r) for r in rows]

    @router.get(
        "/source_credibility/{host}",
        response_model=SourceCredibilityRow,
        deprecated=True,
        dependencies=[Depends(sunset_headers(SUCCESSOR_ROUTE))],
    )
    async def get_source_credibility(
        host: str,
        _principal: str = Depends(require_bearer),
        deps_: RegistryAPIDeps = Depends(_get_deps),
    ) -> SourceCredibilityRow:
        async with deps_.descriptor_registry.pg.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT source_host, score, score_rationale,
                       scored_by, last_updated
                  FROM source_credibility
                 WHERE source_host = $1
                """,
                host,
            )
        if row is None:
            raise HTTPException(
                status_code=404,
                detail=f"source_credibility row for host {host!r} not found",
            )
        return _row_out(row)

    @router.put(
        "/source_credibility/{host}",
        response_model=SourceCredibilityRow,
    )
    async def put_source_credibility(
        host: str,
        body: SourceCredibilityUpsert,
        _principal: str = Depends(require_bearer),
        deps_: RegistryAPIDeps = Depends(_get_deps),
    ) -> SourceCredibilityRow:
        out, _inserted = await _upsert_row(
            deps_,
            host=host,
            score=body.score,
            score_rationale=body.score_rationale,
            scored_by=body.scored_by,
        )
        return out

    @router.delete("/source_credibility/{host}")
    async def delete_source_credibility(
        host: str,
        _principal: str = Depends(require_bearer),
        deps_: RegistryAPIDeps = Depends(_get_deps),
    ) -> dict[str, Any]:
        async with deps_.descriptor_registry.pg.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM source_credibility WHERE source_host = $1", host,
            )
        if result == "DELETE 0":
            raise HTTPException(
                status_code=404,
                detail=f"source_credibility row for host {host!r} not found",
            )
        return {"host": host, "removed": True}

    @router.post(
        "/source_credibility/bulk",
        response_model=BulkUploadResult,
    )
    async def bulk_upload_source_credibility(
        request: Request,
        _principal: str = Depends(require_bearer),
        deps_: RegistryAPIDeps = Depends(_get_deps),
    ) -> BulkUploadResult:
        ctype = (request.headers.get("content-type") or "").split(";")[0].strip()
        if ctype != "text/csv":
            raise HTTPException(
                status_code=415,
                detail=(
                    f"bulk upload requires Content-Type: text/csv (got {ctype!r})"
                ),
            )
        blob = await request.body()
        rows = _parse_csv_rows(blob)

        inserted_count = 0
        updated_count = 0
        errors: list[BulkRowError] = []
        async with deps_.descriptor_registry.pg.acquire() as conn:
            async with conn.transaction():
                for idx, raw in enumerate(rows, start=1):
                    parsed = _validate_csv_row(idx, raw)
                    if isinstance(parsed, BulkRowError):
                        errors.append(parsed)
                        continue
                    host, score, rationale, scored_by = parsed
                    try:
                        _, was_inserted = await _upsert_row(
                            deps_,
                            host=host,
                            score=score,
                            score_rationale=rationale,
                            scored_by=scored_by,
                            conn=conn,
                        )
                    except Exception as exc:  # pragma: no cover — DB failure
                        errors.append(
                            BulkRowError(
                                row_number=idx,
                                host=host,
                                error=f"upsert failed: {exc}",
                            )
                        )
                        continue
                    if was_inserted:
                        inserted_count += 1
                    else:
                        updated_count += 1

        return BulkUploadResult(
            inserted=inserted_count,
            updated=updated_count,
            errors=errors,
        )

    return router
