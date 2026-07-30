# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The source-quality ledger read surface (C3 — one organ, four legs).

Two endpoints over the migration-0115 ``source_quality`` view, which merges the
four separately-grown source-quality organs:

  * ``GET /source-quality``                 — the ledger list (one row per
    source known to ANY leg), filterable.
  * ``GET /sources/{source_id}/quality``    — one source, plus the content the
    list deliberately withholds: the full current rating set (visibility
    filtered) and the compiled dossier markdown.

Mounted under ``/api/v1/v3`` (see ``server.py``), so the full paths are
``/api/v1/v3/source-quality`` and ``/api/v1/v3/sources/{id}/quality``.

Merged, not blended
-------------------
The response nests three typed sections whose names state the KIND of
knowledge each carries:

  * ``asserted`` — somebody SAID SO. The Admiralty rubric grade + dossier
    provenance (``source_ratings`` / ``source_dossiers``, migration 0094) and
    the per-HOST credibility score (``source_credibility``, baseline).
  * ``earned``   — our substrate MEASURED it. The contested-claim track record
    (``source_track_records``, migration 0099). ``null`` when the source has
    never appeared in a resolved contention — an honest absence, never a
    neutral 0.5 dressed up as a measurement.
  * ``computed`` — DERIVED from observed production. Signal recency/volume and
    the A7 freshness grade against the source's own declared cadence.

There is deliberately no composite score. An asserted ``A1`` and an earned
0.31 win-rate answer different questions; averaging them would destroy the one
property the A6 program was built to preserve. A reader gets all three and
decides.

The freshness grade is computed HERE, not in the view: its budget derives from
a cron expression through croniter (Python, not SQL). The view publishes the
inputs; :mod:`legba.data.registry.source_freshness` — the same module the
System Status route uses — grades them. One grading implementation, two
readers.

Deprecation window
------------------
This surface supersedes ``GET /api/v1/v3/sources/{id}/assurance`` and the
``GET /api/v1/source_credibility`` reads. Those KEEP SERVING their original
wire shapes, now stamped ``Deprecation`` / ``Sunset`` / ``Link`` (see
:func:`legba.data.registry.api.sunset_headers`); a redirect was rejected
because the merged body is a different shape and a 301 would break callers
silently. The credibility WRITE routes (PUT / DELETE / bulk) are NOT
deprecated — this is a read surface and has no successor for them.

HARD rule (A6, unchanged): nothing on this surface feeds the faithfulness
score. Trust is not groundedness. The arbiter's earned tie-break seam
recomputes its weight live under the acyclicity guard and never reads this
view.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from . import source_freshness
from .api import RegistryAPIDeps, require_bearer
from .source_assurance_api import (
    SourceDossierOut,
    SourceEarnedOut,
    SourceRatingOut,
    _as_obj,
    _rating_out,
)

logger = logging.getLogger(__name__)

#: Retired template / autowire junk descriptors — excluded from the LIST read
#: exactly as ``/v3/system/source-firing`` excludes them, so the ledger and the
#: status matrix agree on what counts as a real source. The VIEW deliberately
#: keeps them (a ledger that silently drops rows is not a ledger); the filter
#: belongs to the read.
JUNK_DESCRIPTOR_PREFIXES: tuple[str, ...] = (
    "src_autowire_p13_",
    "src_locked_p13_",
    "src_template_p13_",
    "src_tmpl_aw_",
    "src_tmpl_ds_",
    "src_disc_",
)

_MAX_LIST = 1000

_VIEW_MISSING_DETAIL = (
    "source_quality view unavailable (migration 0115 not applied) — the "
    "source-quality ledger cannot be served"
)


# ---------------------------------------------------------------------------
# Wire shapes
# ---------------------------------------------------------------------------


class AssertedQualityOut(BaseModel):
    """Somebody SAID SO — the asserted leg (Admiralty grades + host scores)."""

    #: Admiralty reliability (A-F) / credibility (1-6) of the CURRENT public
    #: fully-graded rating, and the display concatenation (e.g. ``"B2"``).
    admiralty_reliability: Optional[str] = None
    admiralty_credibility: Optional[str] = None
    admiralty_grade: Optional[str] = None
    admiralty_rater: Optional[str] = None
    admiralty_method: Optional[str] = None
    admiralty_rated_at: Optional[datetime] = None
    #: Current rating counts by visibility class. The private COUNT is public
    #: (knowing an annex rating exists leaks nothing); private CONTENT stays
    #: behind the detail route's ``include_private`` opt-in.
    public_rating_count: int = 0
    private_rating_count: int = 0
    has_dossier: bool = False
    dossier_compiled_at: Optional[datetime] = None
    dossier_compiled_by: Optional[str] = None
    #: The host-keyed credibility leg. ``host_matched`` is the row that
    #: actually matched — which may be a PARENT domain of ``endpoint_host``
    #: (``www.csis.org`` -> ``csis.org``), so a reader can see what was scored
    #: rather than assuming an exact hit.
    host_matched: Optional[str] = None
    host_score: Optional[float] = None
    host_tier: Optional[str] = None
    host_state_affiliation: Optional[bool] = None
    host_rationale: Optional[str] = None
    host_scored_by: Optional[str] = None
    host_scored_at: Optional[datetime] = None


class ComputedQualityOut(BaseModel):
    """DERIVED from observed production — the computed leg (A7 freshness)."""

    #: ``ok`` / ``stale`` / ``warn`` / ``empty`` / ``ungraded`` — graded against
    #: a budget derived from this source's OWN declared cadence, never a global
    #: window. ``ungraded`` when no parsable cadence exists or the head is not
    #: active; never faked to ``ok``.
    freshness_grade: str = "ungraded"
    #: The derived budget in minutes; ``null`` exactly when none was derivable.
    budget_minutes: Optional[int] = None
    #: The declared cron the budget came from.
    cadence_raw: Optional[str] = None
    last_signal_at: Optional[datetime] = None
    age_seconds: Optional[int] = None
    signals_24h: int = 0
    signals_7d: int = 0


class SourceQualityRow(BaseModel):
    """One ledger row — the three legs, typed and kept apart."""

    source_id: str
    #: Whether a HEAD ``source_descriptors`` row exists. False rows are real:
    #: a catalog rating may precede registration, and a track record outlives a
    #: retired descriptor.
    registered: bool
    declared_state: Optional[str] = None
    declared_kind: Optional[str] = None
    endpoint_host: Optional[str] = None
    asserted: AssertedQualityOut
    #: ``null`` when the source has no resolved-contest sample at all.
    earned: Optional[SourceEarnedOut] = None
    computed: ComputedQualityOut


class SourceQualityDetail(SourceQualityRow):
    """``GET /sources/{id}/quality`` — the row plus withheld content."""

    #: Echo of the visibility filter this response was computed under.
    includes_private: bool = False
    #: Every CURRENT rating (one per rater x visibility class), not just the
    #: single graded winner the row's ``asserted`` section summarizes.
    ratings: list[SourceRatingOut] = Field(default_factory=list)
    dossier: Optional[SourceDossierOut] = None


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

_COLUMNS = """
    source_id, registered, declared_state, declared_kind, cadence_raw,
    endpoint_url, endpoint_host,
    asserted_admiralty_reliability, asserted_admiralty_credibility,
    asserted_admiralty_grade, asserted_admiralty_rater,
    asserted_admiralty_method, asserted_admiralty_rated_at,
    asserted_public_rating_count, asserted_private_rating_count,
    asserted_has_dossier, asserted_dossier_compiled_at,
    asserted_dossier_compiled_by,
    asserted_host_matched, asserted_host_score, asserted_host_tier,
    asserted_host_state_affiliation, asserted_host_rationale,
    asserted_host_scored_by, asserted_host_scored_at,
    earned_wins, earned_losses, earned_contested_total, earned_win_rate_raw,
    earned_win_rate_smoothed, earned_win_rate_lower, earned_low_sample,
    earned_corroborated, earned_corroboration_total, earned_corroboration_rate,
    earned_lag_hours, earned_sample_as_of, earned_computed_at,
    computed_last_signal_at, computed_age_seconds, computed_signals_24h,
    computed_signals_7d
"""

#: List read. ``$1`` source-id substring (case-insensitive, null = all), ``$2``
#: junk-prefix array, ``$3`` limit. Ordering puts the sources a reader most
#: needs to judge first: contested ones (they have a measured record), then
#: graded ones, then the rest alphabetically.
_LIST_SQL = f"""
    SELECT {_COLUMNS}
      FROM public.source_quality
     WHERE ($1::text IS NULL OR source_id ILIKE '%' || $1 || '%')
       AND NOT (source_id LIKE ANY ($2::text[]))
     ORDER BY earned_contested_total DESC NULLS LAST,
              (asserted_admiralty_grade IS NULL),
              source_id
     LIMIT $3
"""

_GET_SQL = f"SELECT {_COLUMNS} FROM public.source_quality WHERE source_id = $1"

_RATINGS_SQL = """
    SELECT id, source_id, rater, visibility_class, method,
           admiralty_reliability, admiralty_credibility, rubric, refs, rated_at
      FROM public.source_ratings
     WHERE source_id = $1
       AND superseded_by IS NULL
       AND (visibility_class = 'public' OR $2)
     ORDER BY rated_at DESC, rater
"""

_DOSSIER_SQL = """
    SELECT id, source_id, dossier_md, refs, compiled_by, compiled_at
      FROM public.source_dossiers
     WHERE source_id = $1
       AND superseded_by IS NULL
"""


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------


def _get_deps(request: Request) -> RegistryAPIDeps:
    deps = getattr(request.app.state, "registry_deps", None)
    if deps is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "source_quality api not configured "
                "(missing RegistryAPIDeps on app.state)"
            ),
        )
    return deps


def _asserted(row: Any) -> AssertedQualityOut:
    score = row["asserted_host_score"]
    return AssertedQualityOut(
        admiralty_reliability=row["asserted_admiralty_reliability"],
        admiralty_credibility=row["asserted_admiralty_credibility"],
        admiralty_grade=row["asserted_admiralty_grade"],
        admiralty_rater=row["asserted_admiralty_rater"],
        admiralty_method=row["asserted_admiralty_method"],
        admiralty_rated_at=row["asserted_admiralty_rated_at"],
        public_rating_count=int(row["asserted_public_rating_count"] or 0),
        private_rating_count=int(row["asserted_private_rating_count"] or 0),
        has_dossier=bool(row["asserted_has_dossier"]),
        dossier_compiled_at=row["asserted_dossier_compiled_at"],
        dossier_compiled_by=row["asserted_dossier_compiled_by"],
        host_matched=row["asserted_host_matched"],
        host_score=float(score) if score is not None else None,
        host_tier=row["asserted_host_tier"],
        host_state_affiliation=row["asserted_host_state_affiliation"],
        host_rationale=row["asserted_host_rationale"],
        host_scored_by=row["asserted_host_scored_by"],
        host_scored_at=row["asserted_host_scored_at"],
    )


def _earned(row: Any) -> SourceEarnedOut | None:
    """The earned section, or ``None`` when NO track record exists.

    A record row with ``contested_total = 0`` is still a real record (the
    analyst saw the source but it was never contested) and is returned — the
    caller reads ``low_sample`` / ``win_rate_raw is None`` for that. Only the
    total absence of a row is ``None``.
    """
    if row["earned_computed_at"] is None:
        return None
    return SourceEarnedOut(
        wins=int(row["earned_wins"] or 0),
        losses=int(row["earned_losses"] or 0),
        contested_total=int(row["earned_contested_total"] or 0),
        win_rate_raw=row["earned_win_rate_raw"],
        win_rate_smoothed=row["earned_win_rate_smoothed"],
        win_rate_lower=row["earned_win_rate_lower"],
        low_sample=bool(row["earned_low_sample"]),
        corroborated=int(row["earned_corroborated"] or 0),
        corroboration_total=int(row["earned_corroboration_total"] or 0),
        corroboration_rate=row["earned_corroboration_rate"],
        lag_hours=row["earned_lag_hours"],
        sample_as_of=row["earned_sample_as_of"],
        computed_at=row["earned_computed_at"],
    )


def _computed(row: Any) -> ComputedQualityOut:
    """The computed leg — the view's production inputs, graded by the SHARED
    A7 module (never a second, drifting copy of the grading rule)."""
    cadence_raw = row["cadence_raw"]
    age = row["computed_age_seconds"]
    age_int = int(age) if age is not None else None
    budget_minutes = source_freshness.derive_budget_minutes(cadence_raw)
    grade = source_freshness.grade_freshness(
        state=row["declared_state"],
        age_seconds=age_int,
        budget_minutes=budget_minutes,
    )
    return ComputedQualityOut(
        freshness_grade=grade,
        budget_minutes=budget_minutes,
        cadence_raw=cadence_raw,
        last_signal_at=row["computed_last_signal_at"],
        age_seconds=age_int,
        signals_24h=int(row["computed_signals_24h"] or 0),
        signals_7d=int(row["computed_signals_7d"] or 0),
    )


def quality_row(row: Any) -> SourceQualityRow:
    """Project one ``source_quality`` view row onto the wire shape."""
    return SourceQualityRow(
        source_id=row["source_id"],
        registered=bool(row["registered"]),
        declared_state=row["declared_state"],
        declared_kind=row["declared_kind"],
        endpoint_host=row["endpoint_host"],
        asserted=_asserted(row),
        earned=_earned(row),
        computed=_computed(row),
    )


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def build_source_quality_router(deps: RegistryAPIDeps) -> APIRouter:
    router = APIRouter(tags=["source-quality"])

    @router.get("/source-quality", response_model=list[SourceQualityRow])
    async def list_source_quality(
        source_id: str | None = Query(
            default=None,
            description=(
                "Optional case-insensitive substring match on the source id. "
                "Unset returns every non-junk source any leg knows."
            ),
        ),
        graded_only: bool = Query(
            default=False,
            description="Only sources carrying a current public Admiralty grade.",
        ),
        contested_only: bool = Query(
            default=False,
            description=(
                "Only sources with a MEASURED record (contested_total > 0) — "
                "the earned leg's non-empty sample."
            ),
        ),
        freshness_grade: str | None = Query(
            default=None,
            description=(
                "Filter on the computed A7 grade (ok|stale|warn|empty|"
                "ungraded). Applied after grading, which happens in Python."
            ),
        ),
        limit: int = Query(default=500, ge=1, le=_MAX_LIST),
        _principal: str = Depends(require_bearer),
        deps_: RegistryAPIDeps = Depends(_get_deps),
    ) -> list[SourceQualityRow]:
        """The ledger list — one row per source, three legs kept apart.

        Junk template/autowire descriptors are excluded (the same list
        ``/v3/system/source-firing`` uses). A source with no ratings, no track
        record and no signals still appears, with every leg empty: "we know
        nothing about this source" is itself the honest answer, and hiding it
        would make an unrated source indistinguishable from an absent one.
        """
        junk = [f"{p}%" for p in JUNK_DESCRIPTOR_PREFIXES]
        try:
            async with deps_.descriptor_registry.pg.acquire() as conn:
                rows = await conn.fetch(_LIST_SQL, source_id, junk, limit)
        except asyncpg.UndefinedTableError as exc:
            logger.warning("source_quality.view_missing err=%s", exc)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=_VIEW_MISSING_DETAIL,
            ) from exc

        out = [quality_row(r) for r in rows]
        if graded_only:
            out = [r for r in out if r.asserted.admiralty_grade is not None]
        if contested_only:
            out = [
                r for r in out
                if r.earned is not None and r.earned.contested_total > 0
            ]
        if freshness_grade:
            out = [r for r in out if r.computed.freshness_grade == freshness_grade]
        return out

    @router.get(
        "/sources/{source_id}/quality", response_model=SourceQualityDetail,
    )
    async def get_source_quality(
        source_id: str,
        include_private: bool = Query(
            default=False,
            description=(
                "Opt in to private-annex rating rows. Single-operator today; "
                "becomes an entitlement check when multi-user auth lands (the "
                "default-deny shape is what makes that a drop-in)."
            ),
        ),
        _principal: str = Depends(require_bearer),
        deps_: RegistryAPIDeps = Depends(_get_deps),
    ) -> SourceQualityDetail:
        """One source's full quality ledger.

        404 exactly when NO leg knows the id — no head descriptor, no current
        rating, no dossier, no track record. That is the view's own row spine,
        so the gate cannot drift from what the ledger contains.
        """
        try:
            async with deps_.descriptor_registry.pg.acquire() as conn:
                row = await conn.fetchrow(_GET_SQL, source_id)
                if row is None:
                    raise HTTPException(
                        status_code=404,
                        detail=(
                            f"source {source_id!r}: no descriptor, ratings, "
                            "dossier, or earned record known"
                        ),
                    )
                rating_rows = await conn.fetch(
                    _RATINGS_SQL, source_id, include_private,
                )
                dossier_row = await conn.fetchrow(_DOSSIER_SQL, source_id)
        except asyncpg.UndefinedTableError as exc:
            logger.warning("source_quality.view_missing err=%s", exc)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=_VIEW_MISSING_DETAIL,
            ) from exc

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
        base = quality_row(row)
        return SourceQualityDetail(
            **base.model_dump(),
            includes_private=include_private,
            ratings=[_rating_out(r) for r in rating_rows],
            dossier=dossier,
        )

    return router


__all__ = [
    "AssertedQualityOut",
    "ComputedQualityOut",
    "JUNK_DESCRIPTOR_PREFIXES",
    "SourceQualityDetail",
    "SourceQualityRow",
    "build_source_quality_router",
    "quality_row",
]
