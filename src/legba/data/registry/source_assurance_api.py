# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Source assurance ledger read surface (P3-1 layers 1+2; P3-3 layer 3 — A6).

One endpoint over the migration-0094 + 0099 tables:

  * ``GET /sources/{source_id}/assurance`` — the source's CURRENT ratings
    (one per rater × visibility class, ``superseded_by IS NULL``) + the
    CURRENT dossier, visibility-filtered, + the EARNED track record (layer 3,
    ``source_track_records``, migration 0099) when one has been computed.

Mounted under ``/api/v1/v3`` beside the telemetry router (see ``server.py``),
so the full path is ``/api/v1/v3/sources/{source_id}/assurance``. Uses the
same ``RegistryAPIDeps`` bundle + ``require_bearer`` gate as the rest of the
surface.

Visibility filtering — TODAY vs the multi-user seam
---------------------------------------------------

Rating rows carry a ``visibility_class`` (``public | private``; private =
corp/gov annex ratings that never ship with the public catalog). The route
returns ONLY public rows unless the request opts in with
``?include_private=1``.

TODAY (single-operator deployment): any authenticated bearer principal may
opt in — the bearer token IS the operator, so the flag is a presentation
choice, not an authorization boundary.

SEAM (documented, deliberately not built): when multi-user auth lands, the
opt-in must be gated on the caller's entitlements (principal → allowed
visibility classes / annex raters), and the default-deny shape of this route
(private rows excluded unless explicitly requested) is what makes that a
drop-in check here rather than an audit of every consumer. Do NOT add new
consumers that read private rows without going through this filter.

HARD rule (A6): assurance grades feed display today and weighting / flags /
tie-breaks later — NEVER the faithfulness score.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from .api import RegistryAPIDeps, load_assurance_grades, require_bearer
from .descriptor import DescriptorPredicate, Family

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Wire shapes
# ---------------------------------------------------------------------------


class SourceRatingOut(BaseModel):
    """One CURRENT ``source_ratings`` row (wire spelling: ``references``)."""

    rating_id: str
    source_id: str
    rater: str
    visibility_class: str
    method: str
    admiralty_reliability: str | None = None
    admiralty_credibility: str | None = None
    #: Display convenience — reliability+credibility (e.g. ``"B2"``) when both
    #: halves are present, else null.
    grade: str | None = None
    rubric: dict[str, Any] = Field(default_factory=dict)
    references: list[dict[str, Any]] = Field(default_factory=list)
    rated_at: datetime


class SourceDossierOut(BaseModel):
    """The CURRENT ``source_dossiers`` row (wire spelling: ``references``)."""

    dossier_id: str
    source_id: str
    dossier_md: str
    references: list[dict[str, Any]] = Field(default_factory=list)
    compiled_by: str
    compiled_at: datetime


class SourceEarnedOut(BaseModel):
    """The EARNED track record (A6 layer 3 — ``source_track_records``, P3-3).

    The MEASURED half of the ledger: how the source's contested claims actually
    fared. Feeds weighting / tie-break / display ONLY — NEVER faithfulness."""

    wins: int
    losses: int
    contested_total: int
    #: Raw wins/contested_total; null at zero sample.
    win_rate_raw: float | None = None
    #: Beta-Bernoulli smoothed win-rate (prior-damped toward 0.5 by sample) —
    #: the primary display + the (flag-gated) arbiter-consumed value.
    win_rate_smoothed: float
    #: Conservative Wilson score lower bound.
    win_rate_lower: float
    #: True below the sample-size floor (thin evidence — read the smoothed/lower
    #: values, not the raw rate).
    low_sample: bool
    corroborated: int
    corroboration_total: int
    corroboration_rate: float | None = None
    #: The circularity-guard lag (hours) the record was computed under: only
    #: contentions surfaced before (computed_at - lag) contributed.
    lag_hours: float
    sample_as_of: datetime
    computed_at: datetime


class SourceAssuranceOut(BaseModel):
    """``GET /sources/{source_id}/assurance`` envelope."""

    source_id: str
    #: Whether a head ``source_descriptors`` row exists for this id (ratings
    #: may legitimately precede registration — catalog seeds).
    registered: bool
    #: Current PUBLIC Admiralty grade (most recent fully-graded public row),
    #: same value the ``/sources`` list projection carries.
    assurance_grade: str | None = None
    #: Echo of the visibility filter this response was computed under.
    includes_private: bool = False
    ratings: list[SourceRatingOut] = Field(default_factory=list)
    dossier: SourceDossierOut | None = None
    #: A6 layer 3 — the EARNED track record (null when none computed yet or the
    #: source has never appeared in a resolved contention).
    earned: SourceEarnedOut | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_deps(request: Request) -> RegistryAPIDeps:
    deps = getattr(request.app.state, "registry_deps", None)
    if deps is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "source_assurance api not configured "
                "(missing RegistryAPIDeps on app.state)"
            ),
        )
    return deps


def _as_obj(value: Any) -> Any:
    """jsonb fetch → Python object (codec-registered pools already decode)."""
    return json.loads(value) if isinstance(value, str) else value


def _rating_out(row: Any) -> SourceRatingOut:
    reliability = row["admiralty_reliability"]
    credibility = row["admiralty_credibility"]
    return SourceRatingOut(
        rating_id=str(row["id"]),
        source_id=row["source_id"],
        rater=row["rater"],
        visibility_class=row["visibility_class"],
        method=row["method"],
        admiralty_reliability=reliability,
        admiralty_credibility=credibility,
        grade=f"{reliability}{credibility}" if reliability and credibility else None,
        rubric=_as_obj(row["rubric"]) or {},
        references=_as_obj(row["refs"]) or [],
        rated_at=row["rated_at"],
    )


async def load_earned_record(pg: Any, source_id: str) -> SourceEarnedOut | None:
    """The source's EARNED track record (A6 layer 3), or None.

    Degrades to ``None`` when the ``source_track_records`` table does not exist
    yet (a registry rolled forward ahead of migration 0099 must not 500 the
    assurance route over an additive display section)."""
    try:
        async with pg.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT wins, losses, contested_total, win_rate_raw,
                       win_rate_smoothed, win_rate_lower, low_sample,
                       corroborated, corroboration_total, corroboration_rate,
                       lag_hours, sample_as_of, computed_at
                  FROM source_track_records
                 WHERE source_id = $1
                """,
                source_id,
            )
    except asyncpg.UndefinedTableError:
        logger.warning(
            "earned record unavailable: source_track_records missing "
            "(migration 0099 not applied) — omitting the earned section",
        )
        return None
    if row is None:
        return None
    return SourceEarnedOut(
        wins=row["wins"],
        losses=row["losses"],
        contested_total=row["contested_total"],
        win_rate_raw=row["win_rate_raw"],
        win_rate_smoothed=row["win_rate_smoothed"],
        win_rate_lower=row["win_rate_lower"],
        low_sample=row["low_sample"],
        corroborated=row["corroborated"],
        corroboration_total=row["corroboration_total"],
        corroboration_rate=row["corroboration_rate"],
        lag_hours=row["lag_hours"],
        sample_as_of=row["sample_as_of"],
        computed_at=row["computed_at"],
    )


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def build_source_assurance_router(deps: RegistryAPIDeps) -> APIRouter:
    router = APIRouter(tags=["source-assurance"])

    @router.get(
        "/sources/{source_id}/assurance", response_model=SourceAssuranceOut,
    )
    async def get_source_assurance(
        source_id: str,
        include_private: bool = Query(
            default=False,
            description=(
                "Opt in to private-annex rating rows. Single-operator today; "
                "becomes an entitlement check when multi-user auth lands "
                "(see module docstring)."
            ),
        ),
        _principal: str = Depends(require_bearer),
        deps_: RegistryAPIDeps = Depends(_get_deps),
    ) -> SourceAssuranceOut:
        pg = deps_.descriptor_registry.pg
        async with pg.acquire() as conn:
            rating_rows = await conn.fetch(
                """
                SELECT id, source_id, rater, visibility_class, method,
                       admiralty_reliability, admiralty_credibility,
                       rubric, refs, rated_at
                  FROM source_ratings
                 WHERE source_id = $1
                   AND superseded_by IS NULL
                   AND (visibility_class = 'public' OR $2)
                 ORDER BY rated_at DESC, rater
                """,
                source_id, include_private,
            )
            dossier_row = await conn.fetchrow(
                """
                SELECT id, source_id, dossier_md, refs, compiled_by,
                       compiled_at
                  FROM source_dossiers
                 WHERE source_id = $1
                   AND superseded_by IS NULL
                """,
                source_id,
            )

        registered = bool(
            await deps_.descriptor_registry.list(
                DescriptorPredicate(
                    family=Family.SOURCE,
                    descriptor_id=source_id,
                    head_only=True,
                    limit=1,
                )
            )
        )
        # Layer 3 may know a source the descriptor/ratings/dossier don't (a
        # source that has appeared in a resolved contention but was never rated)
        # — so it counts toward "known" for the 404 gate.
        earned = await load_earned_record(pg, source_id)
        if not registered and not rating_rows and dossier_row is None and earned is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"source {source_id!r}: no descriptor, ratings, dossier, or "
                    "earned record known"
                ),
            )

        grades = await load_assurance_grades(pg, [source_id])
        dossier = None
        if dossier_row is not None:
            dossier = SourceDossierOut(
                dossier_id=str(dossier_row["id"]),
                source_id=dossier_row["source_id"],
                dossier_md=dossier_row["dossier_md"],
                references=_as_obj(dossier_row["refs"]) or [],
                compiled_by=dossier_row["compiled_by"],
                compiled_at=dossier_row["compiled_at"],
            )
        return SourceAssuranceOut(
            source_id=source_id,
            registered=registered,
            assurance_grade=grades.get(source_id),
            includes_private=include_private,
            ratings=[_rating_out(r) for r in rating_rows],
            dossier=dossier,
            earned=earned,
        )

    return router
