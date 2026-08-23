# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``GET /v3/system/judge-stats`` — the judge's verdict mix, by SERVING PROVIDER.

GLASS-3's one new backend surface. Everything else in the ops deck reads a route
that already existed; this one exists because a number nobody could see was
already known to move the product.

WHY THIS ROUTE EXISTS. ``served_by`` — the upstream provider an OpenRouter-style
router actually dispatched a call to — has been recorded on every LLM receipt
since 2026-08-16 and read by NOTHING. Not one query, not one panel. It matters
because the same model id served by two different providers does not return the
same verdicts: the measured verdict-flip rate across a provider change was 13.6%.
That is a silent, upstream, un-announced input to the faithfulness numbers the
whole product is graded on, and until this route it was observable only by
hand-decoding a JSONB array. Provider drift is now an operator-visible fact.

WHERE THE DATA IS. There is no receipts table. Judge receipts are elements of the
``analyst_traces.llm_calls`` JSONB array, tagged ``leg = 'verify_judge'`` by the
verify actor (``runtime/dapr_actors.py``) — a POSITIONAL tag, deliberately not a
component-id guess, so it survives a judge re-point. Verdicts are
``analyst_outputs`` rows with ``kind = 'critique'`` and the load-bearing
``title LIKE 'Faithfulness verify%'`` prefix, carrying ``judge_status`` at
``data->'data'->'verification'->>'judge_status'``. The two are correlated by
``run_id``: the verify pass is an in-run side-write, so the critique and the judge
calls that produced it share the analyzed analyst's own run. NO MIGRATION: every
field already exists.

THE ATTRIBUTION PROBLEM, and how this route refuses to lie about it. The join is
many-to-many — one run yields several critiques, and one long finding partitions
into several judge calls — so joining traces to critiques directly would multiply
every verdict by its receipt count and report a cube that is pure fiction. The
provider is therefore resolved PER RUN first (one label per ``run_id``), and only
then attached to that run's critiques. Where a run cannot be resolved to a single
provider, it is not guessed. Four sentinel labels, published on the wire in
``sentinels`` so no reader has to hardcode them:

  * ``(mixed)``      — the run's judge calls named MORE THAN ONE provider (a
                       router flip mid-run). There is no per-critique receipt id,
                       so per-critique attribution is genuinely impossible here.
                       Bucketed, never silently assigned to whichever came first.
  * ``(unrouted)``   — receipts exist but carry no ``served_by``. Correct and
                       expected for a direct provider: only a router reports who
                       ultimately served. NOT an error.
  * ``(no receipt)`` — no ``verify_judge`` receipt on the run. The dominant case
                       BY DESIGN: ``deterministic`` and ``unsampled`` verdicts
                       never called an LLM, so they cannot have a serving
                       provider. Also covers rows predating the ``leg`` tag.
  * ``(unknown)``    — ``judge_status`` itself is absent (legacy rows written
                       before the verification block carried the key).

TWO DELIBERATE DIVERGENCES from ``production_gauge_integrity``'s judge-availability
gauge, which aggregates the same population. They are stated here because the two
surfaces WILL disagree and a reader must know it is on purpose:

  1. A NULL ``judge_status`` is reported as ``(unknown)``, not folded into
     ``deterministic``. The gauge folds it because for a health check "an unknown
     grader is not a working one" is the safe read. This is not a health check —
     it is the measurement instrument, and quietly relabelling legacy rows as a
     grader they may not have used would put fiction in the denominator.
  2. ``unsampled`` is a FIRST-CLASS bucket, not excluded. The gauge excludes it
     because the J2 sampling gate deliberately didn't send those, so counting them
     would page a permanent fake outage. Here the sampled share is itself one of
     the things an operator is reading for.

Consequently ``adjudicated_share`` is ``llm / (n - unsampled)`` — ``unsampled``
leaves the denominator (it was never offered to a judge), ``(unknown)`` stays in
it (it is a real verdict that was not LLM-adjudicated).

EVERY METRIC CARRIES ITS n AND ITS SERVING PROVIDER — the standing constraint,
enforced structurally rather than by convention: there is no field on this
response carrying a rate or a mean without the count it was computed over
sitting beside it, and no aggregate above the cell grain that is not keyed by
``served_by``. A mean over zero rows is ``None``, never ``0.0``.

The cube is additionally keyed by ``judge_pipeline_version``, so a window that
straddles a judge-pipeline stamp change shows two rows instead of one pooled
average. Pooling faithfulness across judge swaps is a known way to launder a
regression into a flat line; this grain makes the swap visible in the data.

House rules for the ``/system/*`` family, both observed here: bearer-gated, and a
read failure logs at INFO and returns an honest empty payload at HTTP 200 with
``measured: false`` — never a 500, because a polling panel would hammer it, and
an empty table that SAYS it measured nothing is not the same artifact as an empty
table that reads as all-clear.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from .api import RegistryAPIDeps, require_bearer

logger = logging.getLogger(__name__)

_ROUTE = "/system/judge-stats"

#: Default / maximum trailing window. The lateral unnest over `llm_calls` has no
#: index to lean on and `analyst_traces` grows ~7.2k rows/day, so the window is
#: bounded hard — every gauge that reads receipts does the same.
DEFAULT_WINDOW_DAYS = 14
MAX_WINDOW_DAYS = 90

#: A run's judge calls may start slightly before the critique window opens (the
#: critique is written later in the same run). Widening the TRACE side by one day
#: stops boundary runs from being reported as `(no receipt)` when they have one.
_TRACE_SLACK = "1 day"

#: The critique family. `title LIKE 'Faithfulness verify%'` is load-bearing and
#: pinned by ~13 laterals across the tree — the `Structural verify%` family is a
#: different instrument and is deliberately NOT counted here.
_CRITIQUE_TITLE_PREFIX = "Faithfulness verify%"

# --- Sentinel provider / status labels (published on the wire) --------------
SENTINEL_MIXED = "(mixed)"
SENTINEL_UNROUTED = "(unrouted)"
SENTINEL_NO_RECEIPT = "(no receipt)"
SENTINEL_UNKNOWN_STATUS = "(unknown)"

#: Labels that are NOT the name of a real serving provider.
_PROVIDER_SENTINELS = frozenset(
    {SENTINEL_MIXED, SENTINEL_UNROUTED, SENTINEL_NO_RECEIPT}
)

#: Shipped to the client so the meaning of a sentinel lives in ONE place — the
#: same reason the production gauge publishes its own alert floor on the wire.
SENTINEL_GLOSSARY: dict[str, str] = {
    SENTINEL_MIXED: (
        "The run's judge calls named more than one serving provider; there is no "
        "per-critique receipt, so these verdicts cannot be attributed to one."
    ),
    SENTINEL_UNROUTED: (
        "Receipts carry no served_by — expected for a direct (non-router) "
        "provider, which never reports who ultimately served. Not an error."
    ),
    SENTINEL_NO_RECEIPT: (
        "No verify_judge receipt on the run. Expected for deterministic and "
        "unsampled verdicts, which never called an LLM, and for rows predating "
        "the receipt tag."
    ),
    SENTINEL_UNKNOWN_STATUS: (
        "judge_status is absent on the row (written before the verification "
        "block carried the key). Reported as-is, never folded into deterministic."
    ),
}

#: `judge_status` domain, plus the legacy-NULL sentinel. Order is render order.
JUDGE_STATUSES: tuple[str, ...] = (
    "llm",
    "deterministic",
    "unsampled",
    SENTINEL_UNKNOWN_STATUS,
)

#: `unsampled` leaves the adjudicated denominator — it was never offered to a
#: judge, so counting it would report a permanent fake shortfall.
_NON_ADJUDICABLE = frozenset({"unsampled"})


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------
# Both statements share the `judge_calls` CTE shape: unnest the receipts array,
# keep only the verify-judge leg, coalesce a missing `served_by` to the sentinel
# so the unrouted stratum stays VISIBLE instead of being dropped by a NULL.
#
# The `$1::timestamptz` casts are LOAD-BEARING, not decoration. Left untyped,
# Postgres resolves `$1 - interval '1 day'` by inferring `$1` as an interval too,
# and the statement dies on `timestamptz > interval`. Because this route swallows
# read failures into an honest empty payload (a polling panel must not get a 500),
# that error would have surfaced only as a permanent `measured: false` — a route
# that always says "I measured nothing" and never says why.

_JUDGE_CALLS_CTE = f"""
    judge_calls AS (
        SELECT t.run_id,
               coalesce(c->>'served_by', '{SENTINEL_UNROUTED}') AS provider,
               c->>'status'              AS call_status,
               (c->>'duration_ms')::float8 AS duration_ms,
               t.run_started_at          AS call_at
          FROM public.analyst_traces t
          CROSS JOIN LATERAL jsonb_array_elements(
                 coalesce(t.llm_calls, '[]'::jsonb)) AS c
         WHERE t.run_started_at > $1::timestamptz - interval '{_TRACE_SLACK}'
           AND c->>'leg' = 'verify_judge'
    )
"""

#: The cube: day x served_by x judge_status x judge_pipeline_version.
#:
#: `run_provider` collapses each run to ONE label BEFORE the join, which is what
#: keeps receipt fan-out from multiplying verdict counts. `min(provider)` is safe
#: only in the single-distinct-value branch, where it IS that value.
_CUBE_SQL = f"""
WITH {_JUDGE_CALLS_CTE},
    run_provider AS (
        SELECT run_id,
               CASE WHEN count(DISTINCT provider) = 1
                    THEN min(provider)
                    ELSE '{SENTINEL_MIXED}'
               END AS provider
          FROM judge_calls
         GROUP BY run_id
    ),
    crit AS (
        SELECT date_trunc('day', o.created_at)::date AS day,
               o.run_id,
               coalesce(
                   o.data->'data'->'verification'->>'judge_status',
                   '{SENTINEL_UNKNOWN_STATUS}') AS judge_status,
               coalesce(
                   o.data->'data'->'verification'->>'judge_pipeline_version',
                   '{SENTINEL_UNKNOWN_STATUS}') AS pipeline_version,
               (o.data->'data'->'verification'->>'faithfulness_score')::float8
                   AS faithfulness
          FROM public.analyst_outputs o
         WHERE o.kind = 'critique'
           AND o.title LIKE $3::text
           AND o.created_at > $1::timestamptz
           AND ($2::text IS NULL OR o.data->>'analyzed_analyst_id' = $2)
    )
SELECT crit.day                                        AS day,
       coalesce(rp.provider, '{SENTINEL_NO_RECEIPT}')  AS served_by,
       crit.judge_status                               AS judge_status,
       crit.pipeline_version                           AS pipeline_version,
       count(*)::int                                   AS n,
       count(crit.faithfulness)::int                   AS faithfulness_n,
       sum(crit.faithfulness)::float8                  AS faithfulness_sum
  FROM crit
  LEFT JOIN run_provider rp ON rp.run_id = crit.run_id
 GROUP BY 1, 2, 3, 4
 ORDER BY 1 DESC, 2, 3
"""

#: Call-level receipt stats, grouped by the RAW per-call provider.
#:
#: NOTE the grain difference, which the response documents: a mixed run's CALLS
#: are counted under each real provider that served them, while its CRITIQUES
#: land in `(mixed)`. That is the honest split — the calls really were served by
#: those providers; it is only the verdict attribution that is ambiguous.
_RECEIPTS_SQL = f"""
WITH {_JUDGE_CALLS_CTE}
SELECT provider                                        AS served_by,
       count(*)::int                                   AS calls,
       count(*) FILTER (
           WHERE call_status IS DISTINCT FROM 'success')::int AS call_errors,
       percentile_disc(0.95) WITHIN GROUP (
           ORDER BY duration_ms)                       AS latency_p95_ms,
       min(call_at)                                    AS first_call_at,
       max(call_at)                                    AS last_call_at
  FROM judge_calls
 GROUP BY 1
 ORDER BY 2 DESC
"""


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


def _mean(total: float | None, n: int) -> float | None:
    """A mean over zero rows is absent, never 0.0."""
    if not n or total is None:
        return None
    return round(total / n, 4)


class JudgeStatsCell(BaseModel):
    """One cell of the day x provider x status x pipeline-version cube.

    `n` is the cell's own count; `faithfulness_n` is the subset of those rows that
    actually carried a score (an `unassessable` verdict carries none), so the mean
    is never read against the wrong denominator."""

    day: date
    served_by: str
    judge_status: str
    judge_pipeline_version: str
    n: int = 0
    faithfulness_n: int = 0
    faithfulness_mean: Optional[float] = None


class JudgeStatsProvider(BaseModel):
    """Per-serving-provider rollup over the whole window — THE drift surface.

    `n` counts CRITIQUES attributed to this provider; `judge_calls` counts
    RECEIPTS served by it. They are different grains on purpose (see
    `_RECEIPTS_SQL`) and a sentinel row may carry critiques with no calls."""

    served_by: str
    is_sentinel: bool = False
    n: int = 0
    by_status: dict[str, int] = Field(default_factory=dict)
    adjudicated_n: int = 0
    adjudicated_share: Optional[float] = None
    faithfulness_n: int = 0
    faithfulness_mean: Optional[float] = None
    judge_calls: int = 0
    judge_call_errors: int = 0
    latency_p95_ms: Optional[float] = None
    #: Bounds of the RUNS whose judge calls this provider served, not of the
    #: calls themselves. Receipts carry a per-call `at`, but it is absent on
    #: rows written before that field existed, and a cast over a missing key
    #: would fail the whole read into `measured: false` for a cosmetic bound.
    #: `run_started_at` is on every trace row and is close enough to answer the
    #: question these fields exist for — when did this provider start/stop
    #: serving us.
    first_call_at: Optional[datetime] = None
    last_call_at: Optional[datetime] = None


class JudgePipelineVersionRow(BaseModel):
    """The window's verdicts split by judge-pipeline stamp. More than one row
    here means the window POOLS across a judge swap — read the per-stamp means,
    not the total."""

    judge_pipeline_version: str
    n: int = 0
    providers: list[str] = Field(default_factory=list)
    faithfulness_n: int = 0
    faithfulness_mean: Optional[float] = None


class JudgeStatsTotals(BaseModel):
    """Window totals. `attributed` + `unattributed` == `critiques` always —
    the two are reported separately because "served by a named provider" and
    "we could not say" are different statements."""

    critiques: int = 0
    by_status: dict[str, int] = Field(default_factory=dict)
    attributed: int = 0
    unattributed: int = 0
    providers: int = 0
    adjudicated_n: int = 0
    adjudicated_share: Optional[float] = None
    faithfulness_n: int = 0
    faithfulness_mean: Optional[float] = None
    judge_calls: int = 0
    judge_call_errors: int = 0


class JudgeStatsOut(BaseModel):
    """The whole read. `measured: false` with empty collections is the honest
    degraded return — distinguishable from a healthy engine that judged nothing."""

    generated_at: Optional[datetime] = None
    window_days: int = 0
    measured: bool = False
    #: True when more than one judge-pipeline stamp is present in the window.
    pools_across_pipeline_versions: bool = False
    totals: JudgeStatsTotals = Field(default_factory=JudgeStatsTotals)
    providers: list[JudgeStatsProvider] = Field(default_factory=list)
    pipeline_versions: list[JudgePipelineVersionRow] = Field(default_factory=list)
    cells: list[JudgeStatsCell] = Field(default_factory=list)
    #: Sentinel label -> what it means, so no client hardcodes the glossary.
    sentinels: dict[str, str] = Field(default_factory=lambda: dict(SENTINEL_GLOSSARY))
    judge_statuses: list[str] = Field(default_factory=lambda: list(JUDGE_STATUSES))


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def _blank(window_days: int) -> JudgeStatsOut:
    """The degraded return. Carries the window it TRIED to measure, so an
    operator can tell a failed read from a genuinely quiet fortnight."""
    return JudgeStatsOut(window_days=window_days, measured=False)


class _Acc:
    """Mutable accumulator — sums and counts only; every rate is derived once at
    the end so a mean can never be built over a denominator it did not use."""

    __slots__ = ("n", "by_status", "f_n", "f_sum", "providers")

    def __init__(self) -> None:
        self.n = 0
        self.by_status: dict[str, int] = {}
        self.f_n = 0
        self.f_sum = 0.0
        self.providers: set[str] = set()

    def add(self, status: str, n: int, f_n: int, f_sum: float | None) -> None:
        self.n += n
        self.by_status[status] = self.by_status.get(status, 0) + n
        self.f_n += f_n
        self.f_sum += f_sum or 0.0

    @property
    def adjudicated_n(self) -> int:
        return sum(
            v for k, v in self.by_status.items() if k not in _NON_ADJUDICABLE
        )

    @property
    def adjudicated_share(self) -> float | None:
        denom = self.adjudicated_n
        if denom <= 0:
            return None
        return round(self.by_status.get("llm", 0) / denom, 4)

    @property
    def faithfulness_mean(self) -> float | None:
        return _mean(self.f_sum, self.f_n)


def build_payload(
    cube_rows: list[dict[str, Any]],
    receipt_rows: list[dict[str, Any]],
    *,
    window_days: int,
    generated_at: datetime,
) -> JudgeStatsOut:
    """Fold the two result sets into the response.

    Split out from the route so the aggregation is testable without a substrate
    — the arithmetic (denominators, sentinel classification, the pooled-stamp
    flag) is the part worth pinning, and it should not need Postgres to pin it.
    """
    cells: list[JudgeStatsCell] = []
    per_provider: dict[str, _Acc] = {}
    per_version: dict[str, _Acc] = {}
    overall = _Acc()

    for r in cube_rows:
        provider = r["served_by"]
        status = r["judge_status"]
        version = r["pipeline_version"]
        n = int(r["n"] or 0)
        f_n = int(r["faithfulness_n"] or 0)
        f_sum = r["faithfulness_sum"]

        cells.append(
            JudgeStatsCell(
                day=r["day"],
                served_by=provider,
                judge_status=status,
                judge_pipeline_version=version,
                n=n,
                faithfulness_n=f_n,
                faithfulness_mean=_mean(f_sum, f_n),
            )
        )
        per_provider.setdefault(provider, _Acc()).add(status, n, f_n, f_sum)
        vacc = per_version.setdefault(version, _Acc())
        vacc.add(status, n, f_n, f_sum)
        vacc.providers.add(provider)
        overall.add(status, n, f_n, f_sum)

    receipts = {r["served_by"]: r for r in receipt_rows}

    providers: list[JudgeStatsProvider] = []
    for label in set(per_provider) | set(receipts):
        acc = per_provider.get(label, _Acc())
        rec = receipts.get(label, {})
        p95 = rec.get("latency_p95_ms")
        providers.append(
            JudgeStatsProvider(
                served_by=label,
                is_sentinel=label in _PROVIDER_SENTINELS,
                n=acc.n,
                by_status=dict(acc.by_status),
                adjudicated_n=acc.adjudicated_n,
                adjudicated_share=acc.adjudicated_share,
                faithfulness_n=acc.f_n,
                faithfulness_mean=acc.faithfulness_mean,
                judge_calls=int(rec.get("calls") or 0),
                judge_call_errors=int(rec.get("call_errors") or 0),
                latency_p95_ms=round(float(p95), 1) if p95 is not None else None,
                first_call_at=rec.get("first_call_at"),
                last_call_at=rec.get("last_call_at"),
            )
        )
    # Real providers first, then sentinels; each block by descending volume, so
    # the drift comparison an operator came for is the top of the table.
    providers.sort(key=lambda p: (p.is_sentinel, -p.n, -p.judge_calls, p.served_by))

    versions = [
        JudgePipelineVersionRow(
            judge_pipeline_version=v,
            n=acc.n,
            providers=sorted(acc.providers),
            faithfulness_n=acc.f_n,
            faithfulness_mean=acc.faithfulness_mean,
        )
        for v, acc in sorted(per_version.items(), key=lambda kv: -kv[1].n)
    ]

    attributed = sum(
        p.n for p in providers if not p.is_sentinel
    )
    totals = JudgeStatsTotals(
        critiques=overall.n,
        by_status=dict(overall.by_status),
        attributed=attributed,
        unattributed=overall.n - attributed,
        providers=sum(1 for p in providers if not p.is_sentinel),
        adjudicated_n=overall.adjudicated_n,
        adjudicated_share=overall.adjudicated_share,
        faithfulness_n=overall.f_n,
        faithfulness_mean=overall.faithfulness_mean,
        judge_calls=sum(p.judge_calls for p in providers),
        judge_call_errors=sum(p.judge_call_errors for p in providers),
    )

    return JudgeStatsOut(
        generated_at=generated_at,
        window_days=window_days,
        measured=True,
        pools_across_pipeline_versions=len(versions) > 1,
        totals=totals,
        providers=providers,
        pipeline_versions=versions,
        cells=cells,
    )


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def build_judge_stats_router(deps: RegistryAPIDeps) -> APIRouter:
    router = APIRouter(tags=["system"])

    def _get_deps(request: Request) -> RegistryAPIDeps:
        return getattr(request.app.state, "registry_deps", deps)

    @router.get(_ROUTE, response_model=JudgeStatsOut)
    async def system_judge_stats(
        days: int = Query(
            default=DEFAULT_WINDOW_DAYS, ge=1, le=MAX_WINDOW_DAYS,
            description="Trailing window in days.",
        ),
        analyst_id: str | None = Query(
            default=None,
            description="Restrict to critiques of one analyzed analyst.",
        ),
        _principal: str = Depends(require_bearer),
        deps_: RegistryAPIDeps = Depends(_get_deps),
    ) -> JudgeStatsOut:
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=days)
        try:
            async with deps_.descriptor_registry.pg.acquire() as conn:
                cube = await conn.fetch(
                    _CUBE_SQL, cutoff, analyst_id, _CRITIQUE_TITLE_PREFIX
                )
                receipts = await conn.fetch(_RECEIPTS_SQL, cutoff)
        except Exception as exc:  # noqa: BLE001 — a polling panel must not get a 500
            logger.info("v3.system.judge_stats.unavailable err=%s", exc)
            return _blank(days)

        return build_payload(
            [dict(r) for r in cube],
            [dict(r) for r in receipts],
            window_days=days,
            generated_at=now,
        )

    return router


__all__ = [
    "DEFAULT_WINDOW_DAYS",
    "JUDGE_STATUSES",
    "MAX_WINDOW_DAYS",
    "SENTINEL_GLOSSARY",
    "SENTINEL_MIXED",
    "SENTINEL_NO_RECEIPT",
    "SENTINEL_UNKNOWN_STATUS",
    "SENTINEL_UNROUTED",
    "JudgePipelineVersionRow",
    "JudgeStatsCell",
    "JudgeStatsOut",
    "JudgeStatsProvider",
    "JudgeStatsTotals",
    "build_judge_stats_router",
    "build_payload",
]
