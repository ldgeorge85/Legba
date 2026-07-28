# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``source_track_record`` sub-handler — A6 layer 3, the EARNED source record.

The MEASURED half of the source assurance ledger (P3-3;
planning/PROGRAM_RECOMMENDATIONS_2026-07-24.md §A6). Layers 1+2 (dossiers +
Admiralty rubrics, migration 0094) are ASSERTED facts/opinions about a source.
This layer is the operator's key insight made concrete: a source's grade should
be EARNED — "when this source's claims were contested, how often did
accumulating evidence side with it?" — computed daily from OUR OWN substrate.

The measurement
---------------
Over RESOLVED contention groups (the 0055/0097 ``fact_contention`` sidecar the
detect-only arbiter maintains; ``status='surfaced'`` with a surfaced-winner
value cluster), for each carrying source:

  * a **WIN** = the source carried the surfaced-winner value cluster;
  * a **LOSS** = it carried ONLY losing (non-winner, non-junk) clusters in that
    group.

One outcome per (contention, source) — a chatty source that filed the same
value ten times counts ONCE. A source is mapped to a value cluster through the
same lineage the arbiter uses: ``fact_contention_values.supporting_fact_ids ->
facts.derived_from -> signals.source_id``. Plus a **corroboration** tally: of
the clusters this source carried in resolved groups, how many drew >= 2 distinct
backing sources (independent corroboration accrued).

The win-rate is SMOOTHED so a source with two contests is never rated extreme:
a Beta-Bernoulli posterior mean with a neutral ``Beta(2, 2)`` prior
(``(wins+2)/(contested_total+4)``), prior-damped toward 0.5 by sample size. A
conservative Wilson score lower bound (z=1.96) is stored alongside for display,
and a ``low_sample`` flag fires below the sample floor.

Circularity guard (HARD — operator + program doc both flagged it)
-----------------------------------------------------------------
The earned record feeds the arbiter's tie-break weight, and the tie-break
decides winners — so the record MUST NOT feed back into the cycle that produced
it. Three independent guards:

  (a) **Lag** — only contentions surfaced STRICTLY BEFORE ``now - lag``
      (``LEGBA_CONTENTION_EARNED_LAG_HOURS``, default 72h) count. A dispute that
      resolved in the last lag window has not "settled" and never contributes.
  (b) **Damping** — the consumed signal is the Beta-smoothed rate, which blends
      toward the neutral 0.5 prior BY SAMPLE SIZE (low n -> ~0.5 -> ~0 weight).
  (c) **No self-influence (acyclicity)** — the value the arbiter consumes is
      recomputed LIVE at decision time via :func:`earned_weights_for_sources`,
      which EXCLUDES the very contention being decided. So a contention's
      outcome can never be an input to its own re-decision: the consumed weight
      is a function only of OTHER, older, already-settled disputes. The STORED
      aggregate in ``source_track_records`` (which includes every contention) is
      a DISPLAY surface — the arbiter never reads it.

  Acyclicity in plain terms: the edge ``records -> decision(C)`` is built from
  ``decisions on {contentions != C, surfaced > lag ago}`` — a strictly smaller,
  temporally-prior set than the decisions that produced ``records``. There is no
  cycle because the input set never contains the decision it feeds.

Consumption principle (HARD, A6): this record feeds WEIGHTING / TIE-BREAK /
flags / display ONLY — NEVER the faithfulness score (trust != groundedness).
Nothing in the verify/judge path reads it. The arbiter's consumption is behind
``LEGBA_CONTENTION_EARNED_WEIGHT`` (default 0 = OFF); today we compute + store +
expose the record and wire the OFF-by-default seam, to be flipped later as a
MEASURED step.

Output path
-----------
A ``deterministic`` META analyst on a DAILY cadence: recomputes every source's
record, refreshes ``source_track_records`` wholesale (migration 0099), and
returns an honest per-source distribution as a genuine FINDING (the distribution
IS the measurement product — the calibration_tracking / fact_decay_scan
precedent), which keeps this handler in the FINDING-emitting set the
STRUCTURAL_VERIFY_EXEMPT drift guard asserts. Registered via
``scripts/bringup_register_source_track_record.py`` (descriptor
``descriptors/analyst_source_track_record.yaml``, ships ``state: draft``).
"""
from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence
from uuid import UUID, uuid4

from ....runtime.analyst_method import AnalystMethodResult
from ...provenance.models import FindingPayload

logger = logging.getLogger(__name__)

SUB_HANDLER_NAME = "source_track_record"

# ---------------------------------------------------------------------------
# Tunables — the win-rate math + the circularity lag.
# ---------------------------------------------------------------------------

#: Beta prior pseudo-count (the neutral Beta(alpha0, beta0) prior mass). Split
#: evenly around 0.5: alpha0 = beta0 = PRIOR_STRENGTH / 2. With the default 4.0
#: a 2-win/0-loss source smooths to (2+2)/(4+4)=... actually (2+2)/(2+0+4)=4/6
#: ~= 0.667, NOT 1.0 — "a source with 2 contests isn't rated extreme".
PRIOR_STRENGTH = 4.0

#: Wilson score interval z (1.96 ~= 95% two-sided) for the conservative lower
#: bound stored for display.
WILSON_Z = 1.96

#: Below this many resolved contests the record is flagged ``low_sample`` — the
#: smoothing already damps it, the flag makes the thin sample explicit.
LOW_SAMPLE_THRESHOLD = 5

#: Circularity-guard lag (guard (a)): only contentions surfaced STRICTLY BEFORE
#: ``now - lag`` feed the record. Env-overridable; shared with the arbiter's
#: live consumption path so both legs measure the same settled slice.
LAG_HOURS_ENV = "LEGBA_CONTENTION_EARNED_LAG_HOURS"
DEFAULT_EARNED_LAG_HOURS = 72.0

#: Defensive cap on the number of per-source records stored in one refresh
#: (a pathological substrate can't blow the pass up). Ordered by contested
#: volume desc so the cap, if ever hit, keeps the most-measured sources.
_MAX_RECORDS = 100_000


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("source_track_record.bad_env %s=%r; using %s", name, raw, default)
        return default


def earned_lag_hours() -> float:
    """The circularity lag in hours (env-configurable, >= 0)."""
    return max(_env_float(LAG_HOURS_ENV, DEFAULT_EARNED_LAG_HOURS), 0.0)


# ---------------------------------------------------------------------------
# Win-rate math (pure — unit-tested in isolation).
# ---------------------------------------------------------------------------


def beta_smoothed_rate(wins: int, losses: int, prior_strength: float = PRIOR_STRENGTH) -> float:
    """Beta-Bernoulli posterior MEAN with a neutral ``Beta(a0, a0)`` prior.

    ``a0 = prior_strength / 2`` on each side (prior centered on 0.5). Returns
    ``(wins + a0) / (wins + losses + 2*a0)``. Prior-DAMPED toward 0.5 by sample
    size: zero sample -> exactly 0.5; a 2-0 source -> ~0.667, never 1.0. This is
    the value the (flag-gated) arbiter seam consumes."""
    a0 = max(prior_strength, 0.0) / 2.0
    denom = wins + losses + 2.0 * a0
    if denom <= 0.0:
        return 0.5
    return (wins + a0) / denom


def wilson_lower_bound(wins: int, losses: int, z: float = WILSON_Z) -> float:
    """Wilson score interval LOWER bound on the raw win rate (conservative).

    Handles small n honestly (widens + pulls toward 0.5); returns 0.0 at zero
    sample (no evidence -> no earned floor). Clamped to [0, 1]."""
    n = wins + losses
    if n <= 0:
        return 0.0
    p_hat = wins / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p_hat + z2 / (2.0 * n)) / denom
    margin = (z / denom) * math.sqrt(p_hat * (1.0 - p_hat) / n + z2 / (4.0 * n * n))
    return max(0.0, min(1.0, center - margin))


def earned_side_weight(wins: int, losses: int, prior_strength: float = PRIOR_STRENGTH) -> float:
    """The damped, NON-NEGATIVE per-side earned signal in ``[0, 1]`` the arbiter
    consumes (later multiplied by ``LEGBA_CONTENTION_EARNED_WEIGHT``).

    Folds the prior-damped smoothed rate to reward ONLY an above-neutral track
    record: ``max(0, 2*(smoothed - 0.5))``. A source with no/low sample sits at
    ~0.5 -> ~0 signal (circularity guard (b): damped by sample size); a proven
    source approaches 1.0. Deliberately never NEGATIVE so the arbiter's
    tie-break weight stays a sum of non-negative additive terms (a poor record
    simply adds nothing; it never pushes a side below its corroboration floor).
    """
    smoothed = beta_smoothed_rate(wins, losses, prior_strength)
    return max(0.0, 2.0 * (smoothed - 0.5))


# ---------------------------------------------------------------------------
# The record.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceRecord:
    """One source's earned track record (a row of ``source_track_records``)."""

    source_id: str
    wins: int
    losses: int
    corroborated: int
    corroboration_total: int
    lag_hours: float
    sample_as_of: datetime
    computed_at: datetime

    @property
    def contested_total(self) -> int:
        return self.wins + self.losses

    @property
    def win_rate_raw(self) -> float | None:
        n = self.contested_total
        return (self.wins / n) if n > 0 else None

    @property
    def win_rate_smoothed(self) -> float:
        return beta_smoothed_rate(self.wins, self.losses)

    @property
    def win_rate_lower(self) -> float:
        return wilson_lower_bound(self.wins, self.losses)

    @property
    def low_sample(self) -> bool:
        return self.contested_total < LOW_SAMPLE_THRESHOLD

    @property
    def corroboration_rate(self) -> float | None:
        t = self.corroboration_total
        return (self.corroborated / t) if t > 0 else None

    @property
    def earned_signal(self) -> float:
        """The damped, non-negative arbiter-facing signal (see
        :func:`earned_side_weight`)."""
        return earned_side_weight(self.wins, self.losses)


# ---------------------------------------------------------------------------
# Substrate query — per-source wins/losses/corroboration over resolved groups.
# ---------------------------------------------------------------------------
#
# One outcome per (contention, source). A source is mapped to a value cluster
# via supporting_fact_ids -> facts.derived_from -> signals.source_id (the same
# lineage the arbiter counts distinct sources on). $1 = the settled cutoff
# (now - lag); $2 = the contention to EXCLUDE (acyclicity guard (c); NULL for
# the full stored record); $3 = an optional source-id allowlist (the arbiter
# narrows to the two sides' carriers; NULL for all).
_RECORDS_SQL = """
WITH resolved AS (
    SELECT id AS contention_id
      FROM fact_contention
     WHERE status = 'surfaced'
       AND surfaced_fact_id IS NOT NULL
       AND surfaced_at IS NOT NULL
       AND surfaced_at < $1::timestamptz
       AND ($2::uuid IS NULL OR id <> $2::uuid)
),
cluster_facts AS (
    SELECT fcv.contention_id,
           fcv.value_key,
           fcv.surfaced_winner                 AS is_winner,
           (fcv.distinct_source_count >= 2)    AS corroborated,
           sf.fact_id
      FROM fact_contention_values fcv
      JOIN resolved r ON r.contention_id = fcv.contention_id
     CROSS JOIN LATERAL unnest(fcv.supporting_fact_ids) AS sf(fact_id)
     WHERE fcv.is_junk = false
),
cluster_sources AS (
    SELECT DISTINCT
           cf.contention_id,
           cf.value_key,
           cf.is_winner,
           cf.corroborated,
           s.source_id
      FROM cluster_facts cf
      JOIN facts f ON f.id = cf.fact_id
     CROSS JOIN LATERAL unnest(f.derived_from) AS d(sig)
      JOIN signals s ON s.id = d.sig
     WHERE ($3::text[] IS NULL OR s.source_id = ANY($3::text[]))
),
per_contention_source AS (
    SELECT contention_id, source_id,
           bool_or(is_winner)     AS on_winning_side,
           bool_or(NOT is_winner) AS on_losing_side
      FROM cluster_sources
     GROUP BY contention_id, source_id
),
outcomes AS (
    SELECT source_id,
           count(*) FILTER (WHERE on_winning_side)                        AS wins,
           count(*) FILTER (WHERE on_losing_side AND NOT on_winning_side) AS losses
      FROM per_contention_source
     GROUP BY source_id
),
corr AS (
    SELECT source_id,
           count(*)                             AS corroboration_total,
           count(*) FILTER (WHERE corroborated) AS corroborated
      FROM (SELECT DISTINCT contention_id, value_key, corroborated, source_id
              FROM cluster_sources) cs
     GROUP BY source_id
)
SELECT COALESCE(o.source_id, c.source_id) AS source_id,
       COALESCE(o.wins, 0)                AS wins,
       COALESCE(o.losses, 0)              AS losses,
       COALESCE(c.corroborated, 0)        AS corroborated,
       COALESCE(c.corroboration_total, 0) AS corroboration_total
  FROM outcomes o
  FULL OUTER JOIN corr c ON c.source_id = o.source_id
 ORDER BY (COALESCE(o.wins, 0) + COALESCE(o.losses, 0)) DESC, source_id
 LIMIT {limit}
""".format(limit=_MAX_RECORDS)


async def compute_source_records(
    conn: Any,
    *,
    now: datetime | None = None,
    lag_hours: float | None = None,
    exclude_contention: UUID | None = None,
    source_ids: Sequence[str] | None = None,
) -> list[SourceRecord]:
    """Compute per-source earned records from the resolved contention sidecar.

    Applies the circularity lag (guard (a)) and, when ``exclude_contention`` is
    given, the acyclicity exclusion (guard (c)). ``source_ids`` narrows the scan
    to a specific set (the arbiter's live per-decision path); ``None`` computes
    every source (the daily stored refresh)."""
    now = now or _now()
    lag = earned_lag_hours() if lag_hours is None else max(lag_hours, 0.0)
    cutoff = now - timedelta(hours=lag)
    allowlist = list(source_ids) if source_ids is not None else None
    rows = await conn.fetch(_RECORDS_SQL, cutoff, exclude_contention, allowlist)
    out: list[SourceRecord] = []
    for r in rows:
        sid = r["source_id"]
        if not sid:
            continue
        out.append(
            SourceRecord(
                source_id=str(sid),
                wins=int(r["wins"] or 0),
                losses=int(r["losses"] or 0),
                corroborated=int(r["corroborated"] or 0),
                corroboration_total=int(r["corroboration_total"] or 0),
                lag_hours=lag,
                sample_as_of=cutoff,
                computed_at=now,
            )
        )
    return out


async def earned_weights_for_sources(
    conn: Any,
    source_ids: Iterable[str],
    *,
    now: datetime,
    exclude_contention: UUID | None,
    lag_hours: float | None = None,
) -> dict[str, float]:
    """The arbiter's LIVE, acyclicity-guarded earned signal per source.

    Recomputes each source's damped earned side-weight in ``[0, 1]`` over
    settled contentions (lag guard) EXCLUDING ``exclude_contention`` (self-
    influence guard) — it deliberately NEVER reads the stored
    ``source_track_records`` aggregate, which includes the contention being
    decided. Sources with no settled record are absent (implicit 0.0)."""
    ids = [s for s in dict.fromkeys(source_ids) if s]
    if not ids:
        return {}
    records = await compute_source_records(
        conn,
        now=now,
        lag_hours=lag_hours,
        exclude_contention=exclude_contention,
        source_ids=ids,
    )
    return {rec.source_id: rec.earned_signal for rec in records}


# ---------------------------------------------------------------------------
# Storage — wholesale refresh of the stored records.
# ---------------------------------------------------------------------------

_UPSERT_SQL = """
INSERT INTO source_track_records (
    source_id, wins, losses, contested_total,
    win_rate_raw, win_rate_smoothed, win_rate_lower, low_sample,
    corroborated, corroboration_total, corroboration_rate,
    lag_hours, sample_as_of, computed_at
) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
ON CONFLICT (source_id) DO UPDATE SET
    wins                = EXCLUDED.wins,
    losses              = EXCLUDED.losses,
    contested_total     = EXCLUDED.contested_total,
    win_rate_raw        = EXCLUDED.win_rate_raw,
    win_rate_smoothed   = EXCLUDED.win_rate_smoothed,
    win_rate_lower      = EXCLUDED.win_rate_lower,
    low_sample          = EXCLUDED.low_sample,
    corroborated        = EXCLUDED.corroborated,
    corroboration_total = EXCLUDED.corroboration_total,
    corroboration_rate  = EXCLUDED.corroboration_rate,
    lag_hours           = EXCLUDED.lag_hours,
    sample_as_of        = EXCLUDED.sample_as_of,
    computed_at         = EXCLUDED.computed_at
"""

#: Delete records for sources no longer in the current set (wholesale refresh —
#: a source that aged entirely out of the lagged window leaves no stale row).
#: An empty allowlist deletes every row (correct: no current records).
_PRUNE_SQL = "DELETE FROM source_track_records WHERE source_id <> ALL($1::text[])"


async def store_source_records(conn: Any, records: Sequence[SourceRecord]) -> None:
    """Upsert the current record set and prune sources no longer present, in one
    transaction (readers see the old set or the new set, never a half-refresh)."""
    async with conn.transaction():
        seen: list[str] = []
        for rec in records:
            await conn.execute(
                _UPSERT_SQL,
                rec.source_id,
                rec.wins,
                rec.losses,
                rec.contested_total,
                rec.win_rate_raw,
                rec.win_rate_smoothed,
                rec.win_rate_lower,
                rec.low_sample,
                rec.corroborated,
                rec.corroboration_total,
                rec.corroboration_rate,
                rec.lag_hours,
                rec.sample_as_of,
                rec.computed_at,
            )
            seen.append(rec.source_id)
        await conn.execute(_PRUNE_SQL, seen)


# ---------------------------------------------------------------------------
# Summary finding — honest per-source distribution (the measurement product).
# ---------------------------------------------------------------------------


def _round(v: float | None, n: int = 3) -> float | None:
    return round(v, n) if v is not None else None


def build_summary(records: Sequence[SourceRecord], *, lag_hours: float) -> FindingPayload:
    contested = [r for r in records if r.contested_total > 0]
    well_sampled = [r for r in contested if not r.low_sample]
    low_sample = [r for r in contested if r.low_sample]
    corroborating = [r for r in records if r.corroboration_total > 0]

    mean_smoothed = (
        sum(r.win_rate_smoothed for r in contested) / len(contested)
        if contested else None
    )
    # Rank by the CONSERVATIVE lower bound (then volume) so a thin 1-0 never
    # tops a proven 40-5; honest "not enough evidence" sources sit low.
    ranked = sorted(
        well_sampled,
        key=lambda r: (r.win_rate_lower, r.contested_total),
        reverse=True,
    )
    top = ranked[:5]
    weakest = sorted(
        well_sampled, key=lambda r: (r.win_rate_lower, -r.contested_total)
    )[:5]

    if not contested:
        title = (
            f"Source track record: {len(records)} source(s) seen, "
            "0 with a resolved-contest sample yet (honest zero-state)"
        )
    else:
        title = (
            f"Source track record: {len(contested)} contested source(s) "
            f"({len(well_sampled)} well-sampled, {len(low_sample)} low-sample); "
            f"mean smoothed win-rate {mean_smoothed:.3f}"
        )

    def _line(r: SourceRecord) -> str:
        return (
            f"  {r.source_id}: {r.wins}W/{r.losses}L "
            f"(smoothed {r.win_rate_smoothed:.3f}, lower {r.win_rate_lower:.3f}"
            f"{', low-sample' if r.low_sample else ''}); "
            f"corroboration {r.corroborated}/{r.corroboration_total}"
        )

    body_lines = [
        f"lag_hours={lag_hours} (circularity guard: contentions surfaced "
        f"< now-{lag_hours}h only)",
        f"sources_total={len(records)} contested={len(contested)} "
        f"well_sampled={len(well_sampled)} low_sample={len(low_sample)} "
        f"corroborating={len(corroborating)}",
        "grades feed weighting/tie-break/display ONLY — never faithfulness (A6)",
        "arbiter consumption OFF by default (LEGBA_CONTENTION_EARNED_WEIGHT=0)",
    ]
    if top:
        body_lines.append("most-earned (by Wilson lower bound):")
        body_lines.extend(_line(r) for r in top)
    if weakest and weakest != top:
        body_lines.append("weakest (by Wilson lower bound):")
        body_lines.extend(_line(r) for r in weakest)

    return FindingPayload(
        title=title[:2048],
        body="\n".join(body_lines)[:65536],
        confidence=1.0,
        evidence=[],
        tags=["deterministic", SUB_HANDLER_NAME],
        data={
            "sub_handler": SUB_HANDLER_NAME,
            "lag_hours": lag_hours,
            "sources_total": len(records),
            "contested_sources": len(contested),
            "well_sampled_sources": len(well_sampled),
            "low_sample_sources": len(low_sample),
            "corroborating_sources": len(corroborating),
            "mean_win_rate_smoothed": _round(mean_smoothed),
            "top_earned": [
                {
                    "source_id": r.source_id,
                    "wins": r.wins,
                    "losses": r.losses,
                    "win_rate_smoothed": _round(r.win_rate_smoothed),
                    "win_rate_lower": _round(r.win_rate_lower),
                }
                for r in top
            ],
        },
    )


# ---------------------------------------------------------------------------
# Public handler entry point.
# ---------------------------------------------------------------------------


async def handle(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: Any | None,
) -> AnalystMethodResult:
    """Sub-handler entry point — one global earned-record recompute + store.

    REFUSES LOUD on a missing pool (the sibling deterministic-META contract): a
    measurement that cannot read the substrate must error visibly, never report
    a quiet zero-record run."""
    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    if pool is None:
        raise RuntimeError(
            "source_track_record requires a live deps.pg_pool — refusing to "
            "report a zero-record run without reading the substrate"
        )

    raw_run_id = options.get("run_id")
    try:
        _ = UUID(str(raw_run_id)) if raw_run_id else uuid4()
    except (ValueError, TypeError):
        _ = uuid4()

    now = _now()
    lag = (
        max(float(options["lag_hours"]), 0.0)
        if options.get("lag_hours") is not None
        else earned_lag_hours()
    )

    async with pool.acquire() as conn:
        records = await compute_source_records(conn, now=now, lag_hours=lag)
        await store_source_records(conn, records)

    finding = build_summary(records, lag_hours=lag)
    return AnalystMethodResult(
        finding=finding,
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
    )


__all__ = [
    "SUB_HANDLER_NAME",
    "PRIOR_STRENGTH",
    "WILSON_Z",
    "LOW_SAMPLE_THRESHOLD",
    "LAG_HOURS_ENV",
    "DEFAULT_EARNED_LAG_HOURS",
    "earned_lag_hours",
    "beta_smoothed_rate",
    "wilson_lower_bound",
    "earned_side_weight",
    "SourceRecord",
    "compute_source_records",
    "earned_weights_for_sources",
    "store_source_records",
    "build_summary",
    "handle",
]
