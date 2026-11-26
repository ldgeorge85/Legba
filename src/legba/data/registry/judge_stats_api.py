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

X4 / ESC - ``uncited``. A second, independent block: per analyzed unit
per day, the fact-asserting sentences the deterministic floor counts in the unit's
bodies, how many carry no citation marker, and how many BLUFs went out without
one (``provenance/uncited_share.py``, stamped on the critique by ``actor_critic``).
It exists because the LLM judge forgives most uncited sentences and the floor
counts every one, so the cost of an uncited body is invisible until the judge is
out. Its own ``measured`` flag, like ``health``; critiques written before the
stamp existed are not counted (a missing stamp is not a zero).

House rules for the ``/system/*`` family, both observed here: bearer-gated, and a
read failure logs at INFO and returns an honest empty payload at HTTP 200 with
``measured: false`` — never a 500, because a polling panel would hammer it, and
an empty table that SAYS it measured nothing is not the same artifact as an empty
table that reads as all-clear.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, AsyncIterator, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from ..provenance.judge_failure_class import classify_status
from ..provenance.judge_transport import JUDGE_STATUS_PARTIAL
from .api import RegistryAPIDeps, require_bearer
from .read_latency import TTLCache

logger = logging.getLogger(__name__)

_ROUTE = "/system/judge-stats"
#: JUDGE-2 — the health block alone (no cube), for status polls.
_HEALTH_ROUTE = "/system/judge-stats/health"

#: Default / maximum trailing window. The lateral unnest over `llm_calls` has no
#: index to lean on and `analyst_traces` grows ~7.2k rows/day, so the window is
#: bounded hard — every gauge that reads receipts does the same.
DEFAULT_WINDOW_DAYS = 14
MAX_WINDOW_DAYS = 90
#: The health read's default window: long enough to hold a weekend outage AND
#: the last adjudicated critique before it, short enough for a status poll.
DEFAULT_HEALTH_WINDOW_DAYS = 7

#: A run's judge calls may start slightly before the critique window opens (the
#: critique is written later in the same run). Widening the TRACE side by one day
#: stops boundary runs from being reported as `(no receipt)` when they have one.
_TRACE_SLACK = "1 day"

#: Seconds a computed judge-stats payload is reused (R-1).
JUDGE_STATS_TTL_S = 60.0
#: P-3 — for this long past its TTL a payload is still served AT ONCE while one
#: background refresh replaces it (stale-while-revalidate; ``generated_at`` is
#: its as-of). The reads are 0.3-0.9 s warm on live after the P-3 rewrite, but
#: the receipts and health reads must still detoast every judge-receipts array
#: in the window (no index can narrow that), and those TOAST pages fall out of
#: cache between landings: the cold read that started P-3 pulled ~17.9k pages
#: from disk in ~20 s. A landing must not wait on that.
JUDGE_STATS_STALE_S = 600.0

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

#
# P-3 (2026-11-14) — READ COST. Measured on live (EXPLAIN ANALYZE, 14-day
# window: ~38k traces, ~8.8k critiques) the cube took 22.5 s from a cold cache
# and 2.4 s warm. Three causes, each fixed below without changing a result row:
#
#   1. DETOAST PER OPERATOR. Every ``o.data->...`` expression detoasts the whole
#      critique ``data`` document again, and the cube had eight of them (~29
#      buffer touches per critique). The verification block is now extracted
#      ONCE per row, in a ``LATERAL (... OFFSET 0)`` the planner cannot inline
#      back into each use (``_VERIFICATION_ONCE``), and every field is read from
#      that in-memory copy. Same for the streak and the uncited reads.
#   2. THE CUBE UNNESTED EVERY TRACE IN THE WINDOW. Only runs that HAVE a
#      critique can be joined to one, so the cube's trace side now reads just
#      those runs by primary key (``run_id IN (SELECT run_id FROM crit)``) —
#      exactly the rows the LEFT JOIN could ever match. The old plan also ran
#      the whole unnest once per parallel worker (x3) under the Gather.
#   3. A MEMOIZE KEYED ON THE WHOLE ``llm_calls`` DOCUMENT. The lateral
#      ``jsonb_array_elements`` join drew a Memoize node that detoasted and
#      hashed every receipts array just to build its cache key. The verify-judge
#      calls are now pulled with ``jsonb_path_query`` as a set-returning
#      function in the select list (no join, so no Memoize), and the leg filter
#      runs inside the path engine instead of on every emitted element.
#
# ``strict`` keeps the path filter equal to ``c->>'leg' = 'verify_judge'``: in
# strict mode an element without a ``leg`` key, a non-object element, or a
# nested array is an error inside the filter, which is "unknown" — excluded,
# exactly as the ``->>`` comparison excluded it (lax mode would unwrap a nested
# array and could match where the old predicate did not). A non-array
# ``llm_calls`` still raises, as ``jsonb_array_elements`` did.
_VERIFY_JUDGE_PATH = """'strict $[*] ? (@.leg == "verify_judge")'"""

#: The critique's verification block, detoasted once per row (see cause 1).
_VERIFICATION_ONCE = """CROSS JOIN LATERAL (
                SELECT o.data->'data'->'verification' AS v OFFSET 0) x"""


def _judge_calls_cte(trace_filter: str = "") -> str:
    """The ``judge_calls`` CTE, with an optional extra predicate on the trace
    row (``t``) — the cube narrows it to the runs it can join."""
    return f"""
    judge_calls AS (
        SELECT j.run_id,
               coalesce(j.c->>'served_by', '{SENTINEL_UNROUTED}') AS provider,
               j.c->>'status'                AS call_status,
               (j.c->>'duration_ms')::float8 AS duration_ms,
               j.run_started_at              AS call_at
          FROM (SELECT t.run_id, t.run_started_at,
                       jsonb_path_query(
                           coalesce(t.llm_calls, '[]'::jsonb),
                           {_VERIFY_JUDGE_PATH}) AS c
                  FROM public.analyst_traces t
                 WHERE t.run_started_at > $1::timestamptz - interval '{_TRACE_SLACK}'{trace_filter}
               ) j
    )
"""


_JUDGE_CALLS_CTE = _judge_calls_cte()

#: The cube: day x served_by x judge_status x judge_pipeline_version.
#:
#: `run_provider` collapses each run to ONE label BEFORE the join, which is what
#: keeps receipt fan-out from multiplying verdict counts. `min(provider)` is safe
#: only in the single-distinct-value branch, where it IS that value.
#:
#: `crit` comes first so `judge_calls` can be narrowed to its runs (P-3 cause 2).
_CUBE_SQL = f"""
WITH crit AS MATERIALIZED (
        SELECT date_trunc('day', o.created_at)::date AS day,
               o.run_id,
               coalesce(
                   x.v->>'judge_status',
                   '{SENTINEL_UNKNOWN_STATUS}') AS judge_status,
               coalesce(
                   x.v->>'judge_pipeline_version',
                   '{SENTINEL_UNKNOWN_STATUS}') AS pipeline_version,
               (x.v->>'faithfulness_score')::float8
                   AS faithfulness,
               -- H3 (2026-09-25/1) — the alignment audit. Absent on every row
               -- written before this stamp (and on every deterministic /
               -- unsampled row, which never called the judge): coalesced to 0,
               -- never guessed, so a legacy row cannot inflate either share.
               coalesce(
                   (x.v->>'aligned_by_id')::int, 0)
                   AS aligned_by_id,
               coalesce(
                   (x.v->>'aligned_positionally')::int, 0)
                   AS aligned_positionally,
               (coalesce(
                   (x.v->>'miscount_claims')::int, 0)
                   <> 0)::int AS miscounted
          FROM public.analyst_outputs o
          {_VERIFICATION_ONCE}
         WHERE o.kind = 'critique'
           AND o.title LIKE $3::text
           AND o.created_at > $1::timestamptz
           AND ($2::text IS NULL OR o.data->>'analyzed_analyst_id' = $2)
    ),
    {_judge_calls_cte(chr(10) + "                   AND t.run_id IN (SELECT run_id FROM crit)")},
    run_provider AS (
        SELECT run_id,
               CASE WHEN count(DISTINCT provider) = 1
                    THEN min(provider)
                    ELSE '{SENTINEL_MIXED}'
               END AS provider
          FROM judge_calls
         GROUP BY run_id
    )
SELECT crit.day                                        AS day,
       coalesce(rp.provider, '{SENTINEL_NO_RECEIPT}')  AS served_by,
       crit.judge_status                               AS judge_status,
       crit.pipeline_version                           AS pipeline_version,
       count(*)::int                                   AS n,
       count(crit.faithfulness)::int                   AS faithfulness_n,
       sum(crit.faithfulness)::float8                  AS faithfulness_sum,
       sum(crit.aligned_by_id)::int                    AS aligned_by_id,
       sum(crit.aligned_positionally)::int             AS aligned_positionally,
       sum(crit.miscounted)::int                       AS miscount_n
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
# JUDGE-2 (W11b) — THE HEALTH FACT: is the judge answering, and since when not?
# ---------------------------------------------------------------------------
# The 2026-10-02 → 10-05 OpenRouter 402 outage published 1,269 floor-only
# critiques on 10-03/10-04 under a green run counter, and no reader surface said
# so. These two statements are the fact every reader can now show.
#
# Per judge COMPONENT, off the ``verify_judge`` receipts (every chain rung that
# was asked is tagged by position, so a fallback rung appears as its own row):
# the newest success, the newest failure and its status, and how many failures
# came after the newest success (the CURRENT run of failures; 0 when the newest
# call succeeded).
_HEALTH_CALLS_SQL = f"""
WITH calls AS (
    SELECT c->>'component_id'                           AS component_id,
           c->>'status'                                 AS status,
           c->>'http_status'                            AS http_status,
           c->>'model'                                  AS model,
           coalesce(CASE WHEN c->>'at' IS NOT NULL
                         THEN (c->>'at')::timestamptz END,
                    j.run_started_at)                   AS at
      FROM (SELECT t.run_started_at,
                   jsonb_path_query(
                       coalesce(t.llm_calls, '[]'::jsonb),
                       {_VERIFY_JUDGE_PATH}) AS c
              FROM public.analyst_traces t
             WHERE t.run_started_at > $1::timestamptz - interval '1 day'
               AND ($2::text IS NULL OR t.analyst_id = $2)
           ) j
     WHERE coalesce(c->>'component_id', '') <> ''
),
last AS (
    SELECT component_id,
           max(at) FILTER (WHERE status = 'success')               AS last_success_at,
           max(at) FILTER (WHERE status IS DISTINCT FROM 'success') AS last_failure_at
      FROM calls GROUP BY 1
)
SELECT l.component_id,
       l.last_success_at,
       l.last_failure_at,
       count(*)::int                                               AS calls,
       count(*) FILTER (
           WHERE c.status IS DISTINCT FROM 'success'
             AND (l.last_success_at IS NULL OR c.at > l.last_success_at))::int
                                                                   AS consecutive_failures,
       (array_agg(c.http_status ORDER BY c.at DESC)
           FILTER (WHERE c.status IS DISTINCT FROM 'success'))[1]  AS last_failure_http_status,
       (array_agg(c.status ORDER BY c.at DESC)
           FILTER (WHERE c.status IS DISTINCT FROM 'success'))[1]  AS last_failure_status,
       max(c.model)                                                AS model
  FROM last l JOIN calls c USING (component_id)
 GROUP BY l.component_id, l.last_success_at, l.last_failure_at
"""

#: The CURRENT floor-only streak, fleet-wide (``scope = 'fleet'``) and per judge
#: ref (``scope = 'component'``): critiques whose judge was ASKED and did not
#: answer (``deterministic`` + ``judge_error``/``judge_empty`` — an unsampled or
#: flag-off row never asked anyone and neither joins nor breaks a streak), newer
#: than the newest ADJUDICATED critique (``llm`` / ``partial``). No such row ⇒ no
#: streak ⇒ ``unverified_since`` NULL. ``open_at_window_start`` says the streak
#: may be older than the window (nothing adjudicated inside it at all).
_HEALTH_STREAK_SQL = f"""
WITH crit AS (
    SELECT o.created_at,
           coalesce(x.v->>'judge_llm_ref', '') AS ref,
           x.v->>'judge_status'                AS st,
           x.v->>'judge_unavailable_reason'    AS reason
      FROM public.analyst_outputs o
      {_VERIFICATION_ONCE}
     WHERE o.kind = 'critique'
       AND o.title LIKE $3::text
       AND o.created_at > $1::timestamptz
       AND ($2::text IS NULL OR o.data->>'analyzed_analyst_id' = $2)
),
ok_ref AS MATERIALIZED (
    SELECT ref, max(created_at) AS ok_at FROM crit
     WHERE st IN ('llm', 'partial') GROUP BY ref
),
ok_fleet AS MATERIALIZED (
    SELECT max(created_at) AS ok_at FROM crit WHERE st IN ('llm', 'partial')
),
floored AS (
    SELECT * FROM crit
     WHERE st = 'deterministic' AND reason IN ('judge_error', 'judge_empty')
)
SELECT 'fleet'::text AS scope, NULL::text AS ref,
       min(f.created_at) AS unverified_since, count(*)::int AS floor_n,
       bool_or(k.ok_at IS NULL) AS open_at_window_start
  FROM floored f CROSS JOIN ok_fleet k
 WHERE k.ok_at IS NULL OR f.created_at > k.ok_at
UNION ALL
SELECT 'component', f.ref,
       min(f.created_at), count(*)::int, bool_or(r.ok_at IS NULL)
  FROM floored f LEFT JOIN ok_ref r ON r.ref = f.ref
 WHERE r.ok_at IS NULL OR f.created_at > r.ok_at
 GROUP BY f.ref
"""


# ---------------------------------------------------------------------------
# X4 / ESC - THE UNCITED-SENTENCE SHARE, per analyzed unit per day.
# ---------------------------------------------------------------------------
# ``actor_critic`` stamps ``fact_sentences`` / ``uncited_fact_sentences`` /
# ``bluf_marker`` on every UNIT critique's ``verification`` block
# (``provenance/uncited_share.py``): the floor's own count of fact-asserting
# sentences on the body, how many carry no marker, and whether the BLUF does.
# A row stamped before this field existed has no ``fact_sentences`` key and is
# NOT counted (a missing stamp is not a zero), so the first day after a roll
# reads as a partial day - ``reads`` beside every share says how partial.
#: P-3: ``analyzed_analyst_id`` sits outside the verification block, so it is
#: the one other detoast here; it rides in the same once-per-row LATERAL.
#: P-5: the four leaves are read off ``v`` in a second LATERAL, so the GROUP
#: BY's sort carries scalars, not every row's whole verification block. With
#: migration 0257's critique (created_at, id) index the planner reads the
#: window by bitmap and sorts above the join; carrying ``v`` that sort spilled
#: 42 MB to disk (0.49 s -> 0.66 s on a live-sized copy); carrying scalars it
#: is 565 kB in memory (0.44 s). Same rows (compared on that copy).
_UNCITED_SQL = """
SELECT date_trunc('day', o.created_at)::date                         AS day,
       y.analyst_id                                                  AS analyst_id,
       count(*)::int                                                 AS reads,
       sum(y.fact_sentences)::int                                    AS fact_sentences,
       sum(y.uncited_fact_sentences)::int                            AS uncited_fact_sentences,
       count(*) FILTER (WHERE y.bluf_marker
                              IN ('cited', 'uncited'))::int          AS bluf_reads,
       count(*) FILTER (WHERE y.bluf_marker
                              = 'uncited')::int                      AS bluf_uncited_reads
  FROM public.analyst_outputs o
  CROSS JOIN LATERAL (
        SELECT o.data->'data'->'verification' AS v,
               o.data->>'analyzed_analyst_id'  AS analyst_id
        OFFSET 0) x
  CROSS JOIN LATERAL (
        SELECT x.analyst_id,
               (x.v->>'fact_sentences')::int          AS fact_sentences,
               (x.v->>'uncited_fact_sentences')::int  AS uncited_fact_sentences,
               x.v->>'bluf_marker'                    AS bluf_marker
        OFFSET 0) y
 WHERE o.kind = 'critique'
   AND o.title LIKE $3::text
   AND o.created_at > $1::timestamptz
   AND ($2::text IS NULL OR o.data->>'analyzed_analyst_id' = $2)
   AND x.v ? 'fact_sentences'
 GROUP BY 1, 2
 ORDER BY 1 DESC, 2
"""


def _failure_class(http_status: Any, status: Any) -> str | None:
    """The newest failure's class (``judge_failure_class``), from its http status
    when the receipt has one, else from the receipt's status token."""
    if http_status not in (None, ""):
        return classify_status(str(http_status))
    if status in (None, "", "success"):
        return None
    if status in ("timeout", "transient_fail"):
        return "timeout"
    return "error"


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
    #: H3 (2026-09-25/1) — the raw alignment-audit counts, summed over this
    #: cell's rows. Rates over these span STATUS (see `positional_share` /
    #: `miscount_rate` on the rollups below, where a cell's fixed status would
    #: make a rate here either trivial or undefined) so they live one level up.
    aligned_by_id: int = 0
    aligned_positionally: int = 0
    miscount_n: int = 0


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
    #: H3 (2026-09-25/1) — the share of this provider's judge-graded ledger
    #: rows the id arm aligned by POSITION rather than by `claim_index`. Falls
    #: sharply once the 2026-09-24/1 reply contract is live; `None` when this
    #: provider carries no aligned rows at all (every row predates H3).
    positional_share: Optional[float] = None
    #: The share of this provider's ADJUDICATED critiques (`llm` + `partial`)
    #: whose judge reply carried a different verdict count than claims sent —
    #: the defect H3 exists to make SALVAGEABLE, not to make rarer (see
    #: `judge_pipeline_version`'s H3 lineage entry: the rate is expected FLAT).
    miscount_rate: Optional[float] = None
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
    #: H3 (2026-09-25/1) — read this beside `judge_pipeline_version`'s own
    #: written lineage entry for the stamp: before 2026-09-24/1 every aligned
    #: row is positional by construction (no prompt ever asked for an id), so
    #: `positional_share` should read ~1.0 on every earlier stamp and fall on
    #: this one — that is the ONE-GET proof the reply contract is reaching the
    #: model. `miscount_rate` is the control: the lineage declares it FLAT
    #: across this boundary (H3 changes what a miscount DOES, never how often
    #: it happens), so a mover here on this stamp is a different defect.
    positional_share: Optional[float] = None
    miscount_rate: Optional[float] = None


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
    #: H3 (2026-09-25/1) — THE day's proof, window-wide. See the field docs on
    #: `JudgePipelineVersionRow` for how to read the pair; this is the same two
    #: rates pooled over every stamp in the window instead of split by one.
    positional_share: Optional[float] = None
    miscount_rate: Optional[float] = None
    judge_calls: int = 0
    judge_call_errors: int = 0


class JudgeComponentHealth(BaseModel):
    """JUDGE-2 — one judge component's health, off its own receipts and the
    critiques stamped with its ref. Every timestamp is absent (never epoch 0)
    when nothing in the window says it; ``unverified_since`` is NULL whenever
    this component's newest adjudicated critique is newer than its newest
    floor-only one."""

    component_id: str
    model: Optional[str] = None
    calls: int = 0
    last_success_at: Optional[datetime] = None
    last_failure_at: Optional[datetime] = None
    #: ``auth`` | ``credit`` | ``retired`` | ``rate_limited`` | ``server_error`` |
    #: ``timeout`` | ``error`` — the NEWEST failure's class; None = no failure.
    failure_class: Optional[str] = None
    last_failure_http_status: Optional[str] = None
    #: Failures after the newest success — 0 when the newest call succeeded.
    consecutive_failures: int = 0
    unverified_since: Optional[datetime] = None
    unverified_n: int = 0
    #: True when nothing adjudicated by this ref sits in the window at all, so
    #: the streak may have begun before ``unverified_since``.
    open_at_window_start: bool = False


class JudgeHealthOut(BaseModel):
    """JUDGE-2 — "is the checking layer answering, and since when not?"

    ``unverified_since`` (fleet) is the earliest critique in the CURRENT
    floor-only streak: critiques whose judge was asked and did not answer, newer
    than the newest adjudicated critique anywhere. NULL = the judge is answering.
    ``unverified_n`` counts that streak. ``measured: false`` = the read failed
    (never "healthy")."""

    measured: bool = False
    unverified_since: Optional[datetime] = None
    unverified_n: int = 0
    open_at_window_start: bool = False
    components: list[JudgeComponentHealth] = Field(default_factory=list)


class JudgeUncitedUnitDay(BaseModel):
    """X4 / ESC - one analyzed unit on one day: of the fact-asserting sentences
    the deterministic floor counts in that unit's bodies, how many carry no
    citation marker, and how many BLUFs went out without one. ``reads`` is the
    number of stamped critiques the sums are over; every share sits beside the
    count it was computed from, and is absent (never 0.0) over zero."""

    day: date
    analyst_id: str
    reads: int = 0
    fact_sentences: int = 0
    uncited_fact_sentences: int = 0
    uncited_share: Optional[float] = None
    bluf_reads: int = 0
    bluf_uncited_reads: int = 0
    bluf_uncited_share: Optional[float] = None


class JudgeUncitedOut(BaseModel):
    """The uncited-sentence block. Its own ``measured``: a failed read leaves
    the cube intact and says so here - an empty ``rows`` with ``measured: true``
    means no stamped critique in the window, not an all-clear."""

    measured: bool = False
    rows: list[JudgeUncitedUnitDay] = Field(default_factory=list)


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
    #: JUDGE-2 — the health fact (its own ``measured``: a failed health read
    #: leaves the cube intact and says so here).
    health: JudgeHealthOut = Field(default_factory=JudgeHealthOut)
    #: X4 / ESC - the uncited-sentence share per analyzed unit per day
    #: (its own ``measured``, like ``health``).
    uncited: JudgeUncitedOut = Field(default_factory=JudgeUncitedOut)


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

    __slots__ = (
        "n", "by_status", "f_n", "f_sum", "providers",
        "aligned_by_id", "aligned_positionally", "miscount_n",
    )

    def __init__(self) -> None:
        self.n = 0
        self.by_status: dict[str, int] = {}
        self.f_n = 0
        self.f_sum = 0.0
        self.providers: set[str] = set()
        # H3 (2026-09-25/1) — the alignment-audit accumulators.
        self.aligned_by_id = 0
        self.aligned_positionally = 0
        self.miscount_n = 0

    def add(
        self,
        status: str,
        n: int,
        f_n: int,
        f_sum: float | None,
        *,
        aligned_by_id: int = 0,
        aligned_positionally: int = 0,
        miscount_n: int = 0,
    ) -> None:
        self.n += n
        self.by_status[status] = self.by_status.get(status, 0) + n
        self.f_n += f_n
        self.f_sum += f_sum or 0.0
        self.aligned_by_id += aligned_by_id
        self.aligned_positionally += aligned_positionally
        self.miscount_n += miscount_n

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

    @property
    def positional_share(self) -> float | None:
        """H3 (2026-09-25/1) — the share of ALIGNED judge-graded ledger rows
        the id arm matched by POSITION rather than by ``claim_index``. ``None``
        when nothing here ever went through alignment (every row predates H3,
        or the stratum never called the judge)."""
        denom = self.aligned_by_id + self.aligned_positionally
        if denom <= 0:
            return None
        return round(self.aligned_positionally / denom, 4)

    @property
    def miscount_rate(self) -> float | None:
        """H3 — the share of critiques a judge call actually GRADED (``llm`` +
        ``partial``) whose reply carried a different verdict count than claims
        sent. NARROWER than ``adjudicated_n`` on purpose: a ``deterministic``
        row is IN that denominator (it dilutes ``adjudicated_share`` by
        design) but never called the judge, so it cannot carry a miscount and
        must not dilute this rate the same way."""
        denom = self.by_status.get("llm", 0) + self.by_status.get(
            JUDGE_STATUS_PARTIAL, 0
        )
        if denom <= 0:
            return None
        return round(self.miscount_n / denom, 4)


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
        # H3 (2026-09-25/1) — absent on a fixture / a pre-H3 row; `.get` so
        # neither breaks the read into `measured: false`.
        aligned_by_id = int(r.get("aligned_by_id") or 0)
        aligned_positionally = int(r.get("aligned_positionally") or 0)
        miscount_n = int(r.get("miscount_n") or 0)

        cells.append(
            JudgeStatsCell(
                day=r["day"],
                served_by=provider,
                judge_status=status,
                judge_pipeline_version=version,
                n=n,
                faithfulness_n=f_n,
                faithfulness_mean=_mean(f_sum, f_n),
                aligned_by_id=aligned_by_id,
                aligned_positionally=aligned_positionally,
                miscount_n=miscount_n,
            )
        )
        per_provider.setdefault(provider, _Acc()).add(
            status, n, f_n, f_sum,
            aligned_by_id=aligned_by_id,
            aligned_positionally=aligned_positionally,
            miscount_n=miscount_n,
        )
        vacc = per_version.setdefault(version, _Acc())
        vacc.add(
            status, n, f_n, f_sum,
            aligned_by_id=aligned_by_id,
            aligned_positionally=aligned_positionally,
            miscount_n=miscount_n,
        )
        vacc.providers.add(provider)
        overall.add(
            status, n, f_n, f_sum,
            aligned_by_id=aligned_by_id,
            aligned_positionally=aligned_positionally,
            miscount_n=miscount_n,
        )

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
                positional_share=acc.positional_share,
                miscount_rate=acc.miscount_rate,
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
            positional_share=acc.positional_share,
            miscount_rate=acc.miscount_rate,
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
        positional_share=overall.positional_share,
        miscount_rate=overall.miscount_rate,
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


def build_health(
    call_rows: list[dict[str, Any]], streak_rows: list[dict[str, Any]],
) -> JudgeHealthOut:
    """Fold the two health result sets (pure — testable without a substrate)."""
    comps: dict[str, JudgeComponentHealth] = {}
    for r in call_rows:
        cid = str(r["component_id"])
        comps[cid] = JudgeComponentHealth(
            component_id=cid,
            model=r.get("model"),
            calls=int(r.get("calls") or 0),
            last_success_at=r.get("last_success_at"),
            last_failure_at=r.get("last_failure_at"),
            failure_class=(
                _failure_class(r.get("last_failure_http_status"), r.get("last_failure_status"))
                if r.get("last_failure_at") is not None else None
            ),
            last_failure_http_status=(
                str(r["last_failure_http_status"])
                if r.get("last_failure_http_status") not in (None, "") else None
            ),
            consecutive_failures=int(r.get("consecutive_failures") or 0),
        )
    out = JudgeHealthOut(measured=True)
    for r in streak_rows:
        since = r.get("unverified_since")
        n = int(r.get("floor_n") or 0)
        open_ = bool(r.get("open_at_window_start")) and since is not None
        if r.get("scope") == "fleet":
            out.unverified_since, out.unverified_n = since, n
            out.open_at_window_start = open_
            continue
        ref = str(r.get("ref") or "")
        if not ref or since is None:
            continue
        comp = comps.setdefault(ref, JudgeComponentHealth(component_id=ref))
        comp.unverified_since, comp.unverified_n = since, n
        comp.open_at_window_start = open_
    out.components = sorted(comps.values(), key=lambda c: c.component_id)
    return out


def build_uncited(rows: list[dict[str, Any]]) -> JudgeUncitedOut:
    """Fold the uncited-share result set (pure - testable without a substrate)."""
    out = JudgeUncitedOut(measured=True)
    for r in rows:
        facts = int(r.get("fact_sentences") or 0)
        unc = int(r.get("uncited_fact_sentences") or 0)
        bluf = int(r.get("bluf_reads") or 0)
        bluf_unc = int(r.get("bluf_uncited_reads") or 0)
        out.rows.append(
            JudgeUncitedUnitDay(
                day=r["day"],
                analyst_id=str(r["analyst_id"]),
                reads=int(r.get("reads") or 0),
                fact_sentences=facts,
                uncited_fact_sentences=unc,
                uncited_share=round(unc / facts, 4) if facts > 0 else None,
                bluf_reads=bluf,
                bluf_uncited_reads=bluf_unc,
                bluf_uncited_share=round(bluf_unc / bluf, 4) if bluf > 0 else None,
            )
        )
    return out


async def _read_uncited(
    conn: Any, cutoff: datetime, analyst_id: str | None
) -> JudgeUncitedOut:
    try:
        rows = await conn.fetch(
            _UNCITED_SQL, cutoff, analyst_id, _CRITIQUE_TITLE_PREFIX
        )
    except Exception as exc:  # noqa: BLE001 - the block degrades on its own
        logger.info("v3.system.judge_uncited.unavailable err=%s", exc)
        return JudgeUncitedOut(measured=False)
    return build_uncited([dict(r) for r in rows])


async def _read_health(conn: Any, cutoff: datetime, analyst_id: str | None) -> JudgeHealthOut:
    try:
        calls = await conn.fetch(_HEALTH_CALLS_SQL, cutoff, analyst_id)
        streak = await conn.fetch(
            _HEALTH_STREAK_SQL, cutoff, analyst_id, _CRITIQUE_TITLE_PREFIX
        )
    except Exception as exc:  # noqa: BLE001 — the health half degrades on its own
        logger.info("v3.system.judge_health.unavailable err=%s", exc)
        return JudgeHealthOut(measured=False)
    return build_health([dict(r) for r in calls], [dict(r) for r in streak])


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def _cacheable(out: JudgeStatsOut) -> bool:
    """A degraded read (any ``measured: false``) is never cached."""
    return out.measured and out.health.measured and out.uncited.measured


def build_judge_stats_router(deps: RegistryAPIDeps) -> APIRouter:
    # R-1 — a trailing-days aggregate over every critique and judge receipt in
    # the window: the same for every caller, and it moves by a few critiques a
    # minute. Reused for JUDGE_STATS_TTL_S per (days, analyst_id), single
    # flight; ``generated_at`` on the payload is when it was computed (its
    # as-of). A degraded read (any ``measured: false``) is never cached.
    # P-3 — then served stale for JUDGE_STATS_STALE_S while it refreshes.
    stats_cache: TTLCache[JudgeStatsOut] = TTLCache(
        JUDGE_STATS_TTL_S, stale_s=JUDGE_STATS_STALE_S
    )

    @contextlib.asynccontextmanager
    async def _prewarm(_app: Any) -> AsyncIterator[None]:
        """P-3 — compute the console's default read once at startup, in the
        background, so the first landing after a roll is not the one that
        pays for a cold cache. Never blocks or fails startup; cancelled at
        shutdown. (Runs only where the app's lifespan runs — the merged
        router lifespan; a bare test app over ASGI does not start it.)"""
        task = None
        try:
            task = stats_cache.refresh_in_background(
                (DEFAULT_WINDOW_DAYS, None),
                lambda: _compute(deps, DEFAULT_WINDOW_DAYS, None),
                should_cache=_cacheable,
            )
        except Exception as exc:  # noqa: BLE001 - a warm-up is optional
            logger.info("v3.system.judge_stats.prewarm_skipped err=%s", exc)
        try:
            yield
        finally:
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task

    router = APIRouter(tags=["system"], lifespan=_prewarm)

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
        cached = await stats_cache.get_or_compute(
            (days, analyst_id),
            lambda: _compute(deps_, days, analyst_id),
            should_cache=_cacheable,
        )
        return cached.value

    async def _compute(
        deps_: RegistryAPIDeps, days: int, analyst_id: str | None
    ) -> JudgeStatsOut:
        """The four reads, each on its own pooled connection and concurrently
        (R-1: ~6 s of database time in sequence on live, 2026-11-14, bounded
        now by the slowest one). Failure semantics unchanged: the cube or the
        receipts failing is the honest blank; health and uncited degrade on
        their own."""
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=days)
        pool = deps_.descriptor_registry.pg

        async def _fetch(sql: str, *args: Any) -> list[Any]:
            async with pool.acquire() as conn:
                return await conn.fetch(sql, *args)

        async def _on_conn(read: Any) -> Any:
            async with pool.acquire() as conn:
                return await read(conn, cutoff, analyst_id)

        results = await asyncio.gather(
            _fetch(_CUBE_SQL, cutoff, analyst_id, _CRITIQUE_TITLE_PREFIX),
            _fetch(_RECEIPTS_SQL, cutoff),
            _on_conn(_read_health),
            _on_conn(_read_uncited),
            return_exceptions=True,
        )
        cube, receipts, health, uncited = results
        for exc in (cube, receipts):
            if isinstance(exc, BaseException):
                logger.info("v3.system.judge_stats.unavailable err=%s", exc)
                return _blank(days)
        if isinstance(health, BaseException):  # acquire failed: same degrade
            logger.info("v3.system.judge_health.unavailable err=%s", health)
            health = JudgeHealthOut(measured=False)
        if isinstance(uncited, BaseException):
            logger.info("v3.system.judge_uncited.unavailable err=%s", uncited)
            uncited = JudgeUncitedOut(measured=False)

        out = build_payload(
            [dict(r) for r in cube],
            [dict(r) for r in receipts],
            window_days=days,
            generated_at=now,
        )
        out.health = health
        out.uncited = uncited
        return out

    @router.get(_HEALTH_ROUTE, response_model=JudgeHealthOut)
    async def system_judge_health(
        days: int = Query(
            default=DEFAULT_HEALTH_WINDOW_DAYS, ge=1, le=MAX_WINDOW_DAYS,
            description="Trailing window in days.",
        ),
        analyst_id: str | None = Query(
            default=None,
            description="Restrict to one analyzed analyst's critiques and runs.",
        ),
        _principal: str = Depends(require_bearer),
        deps_: RegistryAPIDeps = Depends(_get_deps),
    ) -> JudgeHealthOut:
        """JUDGE-2 — the health block alone (cheap enough for a status poll:
        no cube). Same honesty rule: a failed read is ``measured: false``."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        try:
            async with deps_.descriptor_registry.pg.acquire() as conn:
                return await _read_health(conn, cutoff, analyst_id)
        except Exception as exc:  # noqa: BLE001 — a polling surface must not get a 500
            logger.info("v3.system.judge_health.unavailable err=%s", exc)
            return JudgeHealthOut(measured=False)

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
    "DEFAULT_HEALTH_WINDOW_DAYS",
    "JudgeComponentHealth",
    "JudgeHealthOut",
    "JudgeStatsCell",
    "JudgeStatsOut",
    "JudgeStatsProvider",
    "JudgeStatsTotals",
    "JudgeUncitedOut",
    "JudgeUncitedUnitDay",
    "build_judge_stats_router",
    "build_health",
    "build_payload",
    "build_uncited",
]
