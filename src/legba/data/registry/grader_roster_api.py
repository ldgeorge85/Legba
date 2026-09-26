# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""THE GRADER'S ROSTER — correctness AND coverage for every desk, in one read.

``GET /api/v1/v3/eval/grader_roster?nights=7`` — the nightly correctness
grader's whole fleet on one page: the latest ``unit_correctness`` row per
(``target_id``, ``analyst_id``) inside the window, each desk's trailing means
over that window, and the roster totals. Mounted under ``/api/v1/v3`` beside
the v3 telemetry router (the ``since_api`` / ``source_quality_api`` wiring
convention), SELECT-only, bearer-gated.

WHY THIS ROUTE EXISTS. The per-unit number has had a read surface since G2
(``unit_correctness_api`` → the Inspector badge). The ROSTER had none: there
was nowhere to see that on 2026-09-25 the desk roster ran 49.1% correctness at
7.8% coverage, with 173 of its 232 desks carrying no correctness number at all
because the reference decided nothing they said. A correctness share read
without its coverage beside it is the specific failure this module exists to
make impossible, and it is as true of the fleet as of one desk.

── THE FIVE RULES ──────────────────────────────────────────────────────────

  * **ONE GRAIN AT A TIME.** ``grain`` defaults to ``desk`` and is echoed on
    the response. The ``composition`` grain (``country_composition`` /
    ``country_assessment``) is graded against the SAME references as the desks
    it composes over, so a pooled roster would count those claims twice and
    average a composition's number against its own inputs. The sibling
    per-unit route keeps the grains apart with the same filter; 64 of the 296
    graded units on the live instance are compositions, which is how much a
    silent pooling would have moved.

  * **The pair travels together, roster-wide.** Every correctness figure here
    is emitted beside the coverage figure over the same rows. There is no
    field on this response that carries a correctness number alone.

  * **Two roster numbers, each labelled, neither masquerading as the other.**
    ``correctness_mean_of_desks`` is the unweighted mean over desks — one desk,
    one vote, so a desk with four claims counts as much as one with four
    hundred. ``claims_pooled.correctness_pooled`` pools every claim under the
    roster once. They answer different questions and routinely disagree; both
    ship, named for what they are.

  * **A NULL share is UNMEASURED, never zero.** ``correctness_share`` is NULL
    exactly when the reference decided nothing the desk said (migration 0196's
    own words). Such a desk is counted in ``desks_unmeasured``, excluded from
    both means, and contributes nothing but its ``n_claims`` to the pool. A
    coverage of ``0.0``, by contrast, is a MEASURED zero — the reference bore
    on nothing, and that is a finding — so it is averaged like any other value.

  * **The label is composed once.** The per-desk ``badge`` is
    ``unit_correctness_api.correctness_badge`` — the same function the Inspector
    badge renders — so the roster and the per-unit surface can never print a
    NULL share differently. ``_reference_state`` comes from the same module for
    the same reason: "stale" has one definition, the GRADER's, and it lives in
    the stdlib-only leaf ``data/correctness_reference_currency``.

NOT THE OPERATOR GOLD-SET AXIS. ``GET /v3/eval/correctness`` (``v3_api``) is a
human's read of whether a finding was right, over ``correctness_labels``. This
route is the machine grader's read against an independent reference, over
``unit_correctness``. Two different instruments measuring two different things;
they share a word and nothing else, and they are never pooled. Separate routes,
separate sections on the panel, separate means.

REGISTRY-SLIM. This module imports ``unit_correctness_api`` (itself slim) and
``.api``, and nothing from ``analysts.deterministic_handlers`` — whose import
graph reaches ``feedparser``, which the registry image does not carry. A
subprocess guard in ``tests/data_pkg/test_grader_roster_api.py`` poisons that
module and imports this one, so a convenient future import fails in CI rather
than answering 500 in production.

Reference: ``docs/CORRECTNESS_GRADER.md``, migrations
``0196_unit_correctness_grading.sql`` / ``0197_unit_correctness_reference_age.sql``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from .api import RegistryAPIDeps, require_bearer

# The label machinery, imported rather than re-implemented. `_reference_state`
# and `_float_opt` are private to the sibling by naming convention only; they
# are imported here for the same reason `v3_api` imports `scorecard_reconcile`'s
# reducers — one definition of "stale", one definition of how a NULL numeric
# survives hydration. Re-deriving either here is how the roster and the badge
# would start disagreeing.
from .unit_correctness_api import (
    Grain,
    ReferenceState,
    _float_opt,
    _reference_state,
    correctness_badge,
    single_family_row,
)

#: The grain a roster is over, unless the caller asks for the other one. The
#: two are NEVER pooled — ``country_composition`` and ``country_assessment``
#: compose OVER the eight desks, so a pooled roster would count the same
#: claims twice and average a composition's number against its own inputs.
#: `grain` is echoed on the response for the same reason `nights` is: a filter
#: that silently drops two thirds of the graded units is a filter the reader
#: has to be able to see.
DEFAULT_GRAIN: Grain = "desk"

#: Window bounds. The default is a week because the grader's rotation is
#: weekly; 30 is the hard ceiling because past a month the "trailing mean"
#: stops describing the instrument that is running now.
DEFAULT_NIGHTS = 7
MAX_NIGHTS = 30

#: Rendered verbatim by the panel, like every other honesty note on the eval
#: surface. It says the three things a reader of this section must not get
#: wrong: what the pair means, why there are two roster numbers, and what this
#: measurement is NOT.
HONESTY_NOTE = (
    "Correctness is the share of the claims the independent reference BEARS ON "
    "that it bore out; coverage is how much of what the desk said the reference "
    "bears on at all. Neither is readable without the other — 100% correctness "
    "at 9% coverage is a handful of claims confirmed and nine in ten never "
    "looked at. Two roster figures ship because they answer different "
    "questions: the mean of desks gives every desk one vote, the pooled figure "
    "counts every claim once. A desk whose reference decided nothing reads "
    "\"unmeasured\" and is excluded from both means — it is not a zero. Desks "
    "and compositions are graded against the same references, so they are "
    "never pooled: this roster is one grain at a time. This axis is the "
    "machine grader's, against a reference built without seeing this "
    "platform; it is never pooled with faithfulness, with calibration, or with "
    "the operator gold-set correctness axis."
)


# ---------------------------------------------------------------------------
# Response models — strict, like the surface's siblings.
# ---------------------------------------------------------------------------


class DeskLatest(BaseModel):
    """One desk's NEWEST graded night inside the window."""

    as_of: datetime
    correctness_share: float | None = Field(
        description=(
            "contains / (contains + contradicts). NULL — never 0.0 — when the "
            "reference decided nothing this desk said."
        ),
    )
    coverage_share: float | None = Field(
        description=(
            "(contains + contradicts) / n_claims. A 0.0 here is a MEASURED "
            "zero: the reference bore on nothing. NULL only when the desk "
            "published no claims at all."
        ),
    )
    n_claims: int
    n_contains: int
    n_decided: int = Field(
        description="contains + contradicts — the correctness denominator.",
    )
    n_silent: int = Field(
        description=(
            "Claims the reference never touched. The gap between n_claims and "
            "n_decided is where a thin coverage number comes from."
        ),
    )
    single_family: bool = Field(
        description=(
            "One grader family stood behind this number — a real reading, and "
            "a weaker one. Same predicate the composition gate uses."
        ),
    )
    reference_state: ReferenceState
    reference_age_days: float | None
    badge: str = Field(
        description=(
            "The server-composed line, rendered verbatim — identical in shape "
            "and code path to the Inspector's per-unit badge."
        ),
    )


class DeskWindow(BaseModel):
    """The same desk's trailing behaviour across every graded night in the window."""

    nights_graded: int = Field(
        description=(
            "Distinct UTC days this desk carries a row for inside the window — "
            "not the window length. A desk graded twice tonight and never "
            "again reads 1."
        ),
    )
    correctness_mean: float | None = Field(
        description="Mean over the nights that HAVE a correctness share; NULL when none do.",
    )
    coverage_mean: float | None = Field(
        description="Mean over the nights that have a coverage share; NULL when none do.",
    )


class DeskRosterRow(BaseModel):
    """One (target, desk) pair on the roster."""

    target_id: str
    analyst_id: str
    latest: DeskLatest
    window: DeskWindow


class ClaimsPooled(BaseModel):
    """Every claim under the roster's latest rows, counted ONCE.

    The claim-weighted view: a desk that published four hundred claims moves
    these numbers four hundred times as much as a desk that published one.
    Pooled over the SAME rows the table renders (each desk's latest night), so
    the section's figures and its rows can never describe different populations.
    """

    n_claims: int
    n_decided: int
    n_contains: int
    correctness_pooled: float | None = Field(
        description="n_contains / n_decided. NULL when nothing was decided anywhere.",
    )
    coverage_pooled: float | None = Field(
        description="n_decided / n_claims. NULL when no claim was published at all.",
    )


class RosterTotals(BaseModel):
    """The roster in five numbers, plus the pool. Never one number."""

    desks_graded: int = Field(
        description="Desks whose latest row carries a correctness share.",
    )
    desks_unmeasured: int = Field(
        description=(
            "Desks whose latest row has a NULL correctness share — the "
            "reference decided nothing they said. Excluded from both means. "
            "`desks_graded + desks_unmeasured` is every desk in `desks[]`."
        ),
    )
    correctness_mean_of_desks: float | None = Field(
        description=(
            "Unweighted mean of the graded desks' latest correctness shares — "
            "one desk, one vote. NULL when no desk is graded."
        ),
    )
    coverage_mean_of_desks: float | None = Field(
        description=(
            "Unweighted mean of the latest coverage shares, over every desk "
            "that has one (including the unmeasured desks, whose coverage is a "
            "real number). NULL when no desk has one."
        ),
    )
    claims_pooled: ClaimsPooled


class GraderRoster(BaseModel):
    """``GET /v3/eval/grader_roster`` — the whole response."""

    available: bool = Field(
        description=(
            "False when no desk carries a row inside the window — "
            "\"nothing graded yet\", with no as-of and no figures. Never a "
            "roster of zeros."
        ),
    )
    nights: int = Field(description="The window actually applied, in days.")
    grain: Grain = Field(
        description=(
            "The grain this roster is over — `desk` or `composition`, NEVER "
            "both. A composition composes over the desks beside it, so a "
            "pooled roster would count the same claims twice. Echoed so a "
            "reader can see which population the figures describe."
        ),
    )
    as_of: datetime | None = Field(
        default=None,
        description=(
            "The newest `created_at` among the returned rows — when the grader "
            "last wrote one of these numbers. NULL exactly when `available` is "
            "false."
        ),
    )
    desks: list[DeskRosterRow] = Field(default_factory=list)
    roster: RosterTotals
    honesty_note: str


# ---------------------------------------------------------------------------
# SQL — SELECT only, one statement.
# ---------------------------------------------------------------------------
#
# Three passes over one window CTE: the latest row per desk (`DISTINCT ON`, the
# house "newest per key" idiom), the per-desk window aggregate, and the join
# back to the reference the latest row was graded against. Live EXPLAIN
# (ANALYZE, BUFFERS) over the production table on 2026-09-25 — 1,521 rows, 232
# desk-grain desks: a sequential scan on `unit_correctness` filtering
# `(grain, as_of)`, two quicksorts, a merge join and a pkey index scan per
# surviving row. 16.3 ms / 652 shared buffer hits at nights=7; 22.0 ms at
# nights=30 (the whole table). NO INDEX IS PROPOSED — at this size the scan is
# cheaper than maintaining one would be, and the row set is bounded by the
# number of (target, desk) pairs the grader covers, not by history: the window
# aggregate collapses every night server-side. If that ever changes, the index
# is migration 0221 (0220 is spoken for).
_ROSTER_SQL = """
WITH win AS (
    SELECT uc.target_id, uc.analyst_id, uc.as_of, uc.created_at,
           uc.n_claims, uc.n_contains, uc.n_contradicts, uc.n_silent,
           uc.n_single_family, uc.correctness_share, uc.coverage_share,
           uc.families, uc.reference_age_days, uc.reference_id
      FROM public.unit_correctness uc
     WHERE uc.as_of >= $1
       AND uc.grain = $2
),
latest AS (
    SELECT DISTINCT ON (w.target_id, w.analyst_id) w.*
      FROM win w
     ORDER BY w.target_id, w.analyst_id, w.as_of DESC, w.created_at DESC
),
windowed AS (
    SELECT w.target_id, w.analyst_id,
           count(DISTINCT date_trunc('day', w.as_of))::int AS nights_graded,
           avg(w.correctness_share) AS correctness_mean,
           avg(w.coverage_share)    AS coverage_mean
      FROM win w
     GROUP BY w.target_id, w.analyst_id
)
SELECT l.target_id, l.analyst_id, l.as_of, l.created_at,
       l.n_claims, l.n_contains, l.n_contradicts, l.n_silent,
       l.n_single_family, l.correctness_share, l.coverage_share,
       l.families, l.reference_age_days,
       ur.window_start AS ref_window_start,
       ur.window_end   AS ref_window_end,
       g.nights_graded, g.correctness_mean, g.coverage_mean
  FROM latest l
  JOIN windowed g
    ON g.target_id = l.target_id AND g.analyst_id = l.analyst_id
  JOIN public.unit_references ur ON ur.id = l.reference_id
 ORDER BY l.coverage_share ASC NULLS LAST, l.target_id, l.analyst_id
"""


# ---------------------------------------------------------------------------
# Hydration + the pure roster reduction
# ---------------------------------------------------------------------------


def _desk_row(row: Any) -> DeskRosterRow:
    """One SQL row → one roster row, with the badge composed by the sibling."""
    as_of = row["as_of"]
    n_contains = int(row["n_contains"])
    n_decided = n_contains + int(row["n_contradicts"])
    n_claims = int(row["n_claims"])
    correctness_share = _float_opt(row["correctness_share"])
    coverage_share = _float_opt(row["coverage_share"])
    single = single_family_row(
        row["families"], row["n_single_family"], row["n_claims"]
    )
    # The grader's STORED age wins (migration 0197); `_reference_state` derives
    # from the window only when the column is NULL (a pre-0197 row).
    age_days = _float_opt(row["reference_age_days"])
    state = _reference_state(
        row["ref_window_start"], row["ref_window_end"], as_of, age_days=age_days
    )
    return DeskRosterRow(
        target_id=row["target_id"],
        analyst_id=row["analyst_id"],
        latest=DeskLatest(
            as_of=as_of,
            correctness_share=correctness_share,
            coverage_share=coverage_share,
            n_claims=n_claims,
            n_contains=n_contains,
            n_decided=n_decided,
            n_silent=int(row["n_silent"]),
            single_family=single,
            reference_state=state,
            reference_age_days=age_days,
            badge=correctness_badge(
                correctness_share=correctness_share,
                coverage_share=coverage_share,
                n_contains=n_contains,
                n_decided=n_decided,
                n_claims=n_claims,
                as_of=as_of,
                single_family=single,
                reference_state=state,
                reference_age_days=age_days,
            ),
        ),
        window=DeskWindow(
            nights_graded=int(row["nights_graded"]),
            correctness_mean=_float_opt(row["correctness_mean"]),
            coverage_mean=_float_opt(row["coverage_mean"]),
        ),
    )


def _mean(values: Sequence[float]) -> float | None:
    """The mean, or the honest absence. An empty list is NOT zero."""
    return sum(values) / len(values) if values else None


def roster_totals(desks: Sequence[DeskRosterRow]) -> RosterTotals:
    """Reduce the desk rows to the roster block. PURE — no substrate, so the
    arithmetic that decides what the headline says is testable without a DB.

    Two populations, deliberately different:

      * the MEANS run over desks, skipping a NULL share — a desk the reference
        decided nothing for has no correctness number to average, and coalescing
        it to 0.0 would drag the fleet's headline down by inventing failures out
        of silence;
      * the POOL runs over claims, and every desk contributes its ``n_claims``
        whether or not it produced a share — a silent desk's claims are exactly
        what makes the pooled coverage small, and dropping them would flatter it.
    """
    correctness = [
        d.latest.correctness_share
        for d in desks
        if d.latest.correctness_share is not None
    ]
    coverage = [
        d.latest.coverage_share
        for d in desks
        if d.latest.coverage_share is not None
    ]
    n_claims = sum(d.latest.n_claims for d in desks)
    n_decided = sum(d.latest.n_decided for d in desks)
    n_contains = sum(d.latest.n_contains for d in desks)
    return RosterTotals(
        desks_graded=len(correctness),
        desks_unmeasured=len(desks) - len(correctness),
        correctness_mean_of_desks=_mean(correctness),
        coverage_mean_of_desks=_mean(coverage),
        claims_pooled=ClaimsPooled(
            n_claims=n_claims,
            n_decided=n_decided,
            n_contains=n_contains,
            correctness_pooled=(n_contains / n_decided) if n_decided else None,
            coverage_pooled=(n_decided / n_claims) if n_claims else None,
        ),
    )


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def build_grader_roster_router(deps: RegistryAPIDeps) -> APIRouter:
    """The roster route, bound to the registry deps.

    Mounted by ``server.py`` under ``/api/v1/v3`` beside the telemetry router,
    so it inherits that prefix and the ``require_bearer`` posture the rest of
    the v3 read surface keeps.
    """
    router = APIRouter(tags=["eval"])

    @router.get("/eval/grader_roster", response_model=GraderRoster)
    async def grader_roster(
        nights: int = Query(
            default=DEFAULT_NIGHTS,
            description=(
                "Window in days, counted back from now. 1-30; the trailing "
                "means and `nights_graded` are over this window, the table "
                "rows are each desk's newest night inside it."
            ),
        ),
        grain: Grain = Query(
            default=DEFAULT_GRAIN,
            description=(
                "`desk` or `composition`. The two grains are NEVER pooled; "
                "this filter is how a caller keeps them apart, and the applied "
                "value is echoed on the response."
            ),
        ),
        principal: str = Depends(require_bearer),
    ) -> GraderRoster:
        """Every graded desk's correctness AND coverage, plus the roster totals.

        A desk with no row inside the window is ABSENT from ``desks[]`` — it is
        not a zero, and it is not an unmeasured row either: the grader simply
        has not reached it. An empty roster answers 200 with
        ``available: false`` and no figures at all.

        ONE GRAIN AT A TIME. ``country_composition`` / ``country_assessment``
        are graded against the same references as the desks they compose over;
        pooling the two grains would count those claims twice and average a
        composition's number against its own inputs. The sibling per-unit route
        keeps them apart with the same filter, for the same reason.
        """
        if nights < 1 or nights > MAX_NIGHTS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"nights must be in [1, {MAX_NIGHTS}]",
            )
        cutoff = datetime.now(timezone.utc) - timedelta(days=nights)

        async with deps.descriptor_registry.pg.acquire() as conn:
            rows = await conn.fetch(_ROSTER_SQL, cutoff, grain)

        desks = [_desk_row(r) for r in rows]
        # The stamp is the newest write among the rows actually returned, so it
        # can never describe a run whose numbers are not on this page.
        as_of = max((r["created_at"] for r in rows), default=None)
        return GraderRoster(
            available=bool(desks),
            nights=nights,
            grain=grain,
            as_of=as_of,
            desks=desks,
            roster=roster_totals(desks),
            honesty_note=HONESTY_NOTE,
        )

    return router


__all__ = [
    "DEFAULT_GRAIN",
    "DEFAULT_NIGHTS",
    "HONESTY_NOTE",
    "MAX_NIGHTS",
    "ClaimsPooled",
    "DeskLatest",
    "DeskRosterRow",
    "DeskWindow",
    "GraderRoster",
    "RosterTotals",
    "build_grader_roster_router",
    "roster_totals",
]
