# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``desk_baseline`` sub-handler — P3-7 CAST-recipe per-desk statistical baseline.

A deterministic META analyst on a DAILY cadence that computes, per desk
(``g20`` + ``watch``), a falsifiable QUANTITATIVE PRIOR over OUR OWN substrate
(``signals`` + ``analyst_outputs``) — the shape of the published ACLED CAST
methodology (feature recipe → robust baseline expectation → deviation) but
built entirely internally, with NO external data and NO ACLED dependency. It
persists one baseline per (desk, metric) into the ``desk_baselines`` sidecar
(migration 0103) and emits ONE honest summary finding.

What this is — and is NOT
-------------------------
It is a DESCRIPTIVE statistical baseline: the trailing-window expected rate + an
uncertainty band + whether the CURRENT window deviates beyond it. The deviation
is the useful signal (it feeds the P1-3 ``alert_trigger_scan`` baseline-
deviation trigger as a persistent prior, and gives a desk LLM read a number to
agree or argue with).

It is NOT a forecast. This does not reopen forecasting-as-claim (frozen — the
program's D2 drop / A4 successor). There is NO Brier, NO skill score, NO
probability-of-event: ``expected`` is a trailing MEAN rate, and nothing here is
ever surfaced as a free-text prediction. The summary states that plainly, and
the row NEVER touches a faithfulness score (nothing in the verify/judge path
reads it).

The metrics — a faithful mirror of the trigger
----------------------------------------------
Exactly the two the P1-3 baseline-deviation trigger measures, computed with the
SAME window shape (24h buckets; bucket 0 = current window, 1..N = baseline) and
the SAME WHERE clauses, so this table is a durable mirror of that ephemeral
scan:

  * ``signal_volume_24h``       — ``signals`` whose ``geo`` overlaps the desk's
                                  ``scope.geo`` ISO2 set.
  * ``high_sev_findings_24h``   — ``kind='finding'`` rows on the desk with
                                  severity high/critical (column or tag).

The estimator — dependency-light + robust
------------------------------------------
lightgbm/scipy are NOT in the image (pyproject ships numpy / scikit-learn /
pandas only), and the point of P3-7 is a falsifiable PRIOR, not SOTA
forecasting — so this is a pure-stdlib ``statistics`` estimator, no heavy
model:

  * ``expected``     — trailing MEAN daily rate (the point prior; mean-centred so
                       the band shape matches the trigger it feeds).
  * ``center_median``— trailing median (a spike-robust centre readout).
  * ``robust_sigma`` — ``max(sample_stddev, sqrt(mean))``. The ``sqrt(mean)`` is
                       the POISSON floor (a count process with rate λ has σ=√λ):
                       it stops a steady desk's σ collapsing to ≈0 and the band
                       collapsing with it — the one deliberate robustness gain
                       over the trigger's raw stddev.
  * band             — ``expected ∓ n_sigma·robust_sigma`` (low floored at 0).

Deviation — with the trigger's ABSOLUTE floors
----------------------------------------------
``within`` | ``above`` | ``below``:

  * ``above`` reuses :func:`alert_trigger_scan.baseline_exceeds` VERBATIM —
    ``current >= min_current AND current > expected + n_sigma·robust_sigma`` —
    so a quiet desk's σ≈0 blip (2 signals over a 0.1 mean) can NEVER read as a
    deviation. The absolute floors ARE ``alert_trigger_scan``'s
    (``MIN_CURRENT_SIGNALS`` = 10 / ``MIN_CURRENT_FINDINGS`` = 3), imported so
    the two stay in lockstep.
  * ``below`` fires only when the current window is under ``band_low`` AND the
    BASELINE mean itself clears the floor — an unusually-quiet desk is a signal
    only when it normally is NOT quiet (a collection-gap flavour), never for a
    perennially-quiet desk dropping from 2 to 0.
  * ``insufficient_history`` is a separate HONESTY FLAG (fewer than
    ``MIN_ACTIVE_DAYS`` baseline days had any events). It does NOT suppress an
    absolute-floor ``above`` — a real spike over the floor is still a real
    deviation; the flag only warns the reader the band is weak.

The feature recipe (the rest of the CAST features)
---------------------------------------------------
Stored in the row's ``features`` jsonb (audit provenance of the estimate, not a
query axis): lags at t-1 / t-7 / t-28, rolling means over 7 / 28 days,
``hours_since_last_high_sev`` (time-since-last-high-sev-event), and the
neighbour-desk SPILLOVER — the summed current-window signal volume of the
geographically-adjacent desks, over a coarse static in-code land-border
adjacency (:data:`LAND_ADJACENCY`) used ONLY as a feature input, never surfaced
as a geographic claim.

Registered via ``scripts/bringup_register_desk_baseline.py`` (descriptor
``descriptors/analyst_desk_baseline.yaml``, ships ``state: draft``).
"""
from __future__ import annotations

import logging
import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence
from uuid import UUID, uuid4

from ....runtime.analyst_method import AnalystMethodResult
from ...provenance.models import FindingPayload
from . import alert_trigger_scan as ats

logger = logging.getLogger(__name__)

SUB_HANDLER_NAME = "desk_baseline"

#: The two metrics — the SAME identifiers the P1-3 trigger uses (imported so a
#: rename can never drift the persistent mirror off the ephemeral scan).
METRIC_SIGNAL_VOLUME = ats.METRIC_SIGNAL_VOLUME
METRIC_HIGH_SEV_FINDINGS = ats.METRIC_HIGH_SEV_FINDINGS

#: Trailing baseline depth in 24h buckets (mirrors the trigger's 28d default).
DEFAULT_BASELINE_DAYS = ats.DEFAULT_BASELINE_DAYS
#: Band half-width in robust sigmas (mirrors the trigger's 2σ default).
DEFAULT_N_SIGMA = ats.DEFAULT_BASELINE_SIGMA

#: Absolute current-window floors — THE trigger's floors, imported so the
#: deviation call is byte-for-byte the same anti-false-fire guard.
MIN_CURRENT_SIGNALS = ats.MIN_CURRENT_SIGNALS
MIN_CURRENT_FINDINGS = ats.MIN_CURRENT_FINDINGS

#: A band rests on "thin history" (honesty flag) when fewer than this many of
#: the trailing baseline days carried ANY events — too few to characterise a
#: distribution. Does NOT suppress an absolute-floor exceedance.
MIN_ACTIVE_DAYS = 3

#: Defensive bound on the desk set (g20 + watch is ~32; this is a ceiling).
_MAX_DESKS = 200

#: How many top deviating desks the summary enumerates.
_SUMMARY_TOP_N = 12

#: The honesty frame carried on every summary finding + the eval route.
NO_FORECAST_NOTE = (
    "Descriptive statistical baseline over our own substrate (trailing "
    f"{DEFAULT_BASELINE_DAYS}d, robust mean±{DEFAULT_N_SIGMA:g}σ band with a "
    "Poisson-floored sigma). NOT a forecast/prediction/skill claim and never "
    "surfaced as one — a falsifiable prior each desk read can agree or argue "
    "with. Deviation = current 24h window outside the band, with absolute "
    f"floors (signals>={MIN_CURRENT_SIGNALS} / high-sev>={MIN_CURRENT_FINDINGS}) "
    "mirroring the P1-3 baseline_deviation trigger."
)


# ---------------------------------------------------------------------------
# Static land-border adjacency (in-code — a feature input, NOT a geo claim)
# ---------------------------------------------------------------------------
# A coarse land-border adjacency among the CURRENT g20 + watch desk countries,
# used ONLY to compute the neighbour-desk spillover feature. Edges are listed
# once (canonical) and expanded symmetrically by build_adjacency, so an
# asymmetry bug is impossible; the test asserts symmetry. Countries with no
# desk neighbour (islands, or land borders only with non-desk countries — AU,
# GB, JP, SA, ZA, IL, TW, SD, CD, HT, ID) simply carry no edge and get zero
# spillover (honest degradation). This is public geographic fact expressed as a
# code constant (like the geo_convergence precision sets), NOT curated seed data.
_ADJACENCY_EDGES: tuple[tuple[str, str], ...] = (
    ("AR", "BR"),
    ("CA", "US"),
    ("MX", "US"),
    ("DE", "FR"),
    ("FR", "IT"),
    ("CN", "RU"),
    ("CN", "IN"),
    ("CN", "KP"),
    ("CN", "PK"),
    ("CN", "MM"),
    ("IN", "PK"),
    ("IN", "MM"),
    ("RU", "UA"),
    ("RU", "KP"),
    ("KR", "KP"),
    ("IR", "TR"),
    ("IR", "PK"),
    ("ML", "BF"),
    ("ML", "NE"),
    ("BF", "NE"),
)


def build_adjacency(
    edges: Sequence[tuple[str, str]] = _ADJACENCY_EDGES,
) -> dict[str, frozenset[str]]:
    """Symmetric ISO2 → neighbouring ISO2s map built from the canonical edges."""
    out: dict[str, set[str]] = {}
    for a, b in edges:
        a, b = a.strip().upper(), b.strip().upper()
        out.setdefault(a, set()).add(b)
        out.setdefault(b, set()).add(a)
    return {k: frozenset(v) for k, v in out.items()}


#: The materialized symmetric adjacency (built once at import).
LAND_ADJACENCY: dict[str, frozenset[str]] = build_adjacency()


def neighbor_desks(
    geo: Sequence[str],
    iso2_to_desk: Mapping[str, str],
    *,
    self_desk: str,
    adjacency: Mapping[str, frozenset[str]] = LAND_ADJACENCY,
) -> list[str]:
    """The desk ids adjacent to ``geo`` (deterministic, self excluded).

    For each ISO2 in the desk's ``scope.geo`` we take its adjacency set, map the
    neighbouring ISO2s back to desk ids via ``iso2_to_desk``, drop the desk
    itself, and return a sorted de-duplicated list.
    """
    found: set[str] = set()
    for code in geo:
        if not isinstance(code, str):
            continue
        for nb_iso2 in adjacency.get(code.strip().upper(), frozenset()):
            desk = iso2_to_desk.get(nb_iso2)
            if desk is not None and desk != self_desk:
                found.add(desk)
    return sorted(found)


# ---------------------------------------------------------------------------
# The robust estimator + feature recipe (pure — testable with NO database)
# ---------------------------------------------------------------------------


@dataclass
class BaselineEstimate:
    """The robust baseline expectation + band + deviation for one metric."""

    expected: float
    center_median: float
    robust_sigma: float
    band_low: float
    band_high: float
    n_sigma: float
    sample_days: int
    active_days: int
    insufficient_history: bool
    deviation: str
    deviation_sigma: Optional[float]


def estimate_baseline(
    baseline_counts: Sequence[float],
    current: float,
    *,
    n_sigma: float = DEFAULT_N_SIGMA,
    min_current: float,
    min_active_days: int = MIN_ACTIVE_DAYS,
) -> BaselineEstimate:
    """Robust trailing baseline + band + deviation over the daily counts.

    ``baseline_counts`` are the trailing 24h bucket counts (bucket 1..N;
    zero-filled). ``current`` is the current-window count (bucket 0). The band
    is centred on the trailing mean with a Poisson-floored robust sigma; the
    deviation call reuses the trigger's exact absolute-floor exceedance for
    ``above`` and a floor-gated ``below`` for the unusually-quiet case.
    """
    counts = [float(c) for c in baseline_counts]
    cur = float(current)
    sample_days = len(counts)
    active_days = sum(1 for c in counts if c > 0.0)

    if sample_days == 0:
        # No baseline window at all (defensive — zero-fill normally prevents
        # this). Nothing to be a baseline of; report the honest thin state.
        return BaselineEstimate(
            expected=0.0,
            center_median=0.0,
            robust_sigma=0.0,
            band_low=0.0,
            band_high=0.0,
            n_sigma=n_sigma,
            sample_days=0,
            active_days=0,
            insufficient_history=True,
            deviation="within",
            deviation_sigma=None,
        )

    mean = statistics.fmean(counts)
    median = statistics.median(counts)
    sample_sigma = statistics.stdev(counts) if sample_days >= 2 else 0.0
    poisson_sigma = math.sqrt(mean) if mean > 0.0 else 0.0
    robust_sigma = max(sample_sigma, poisson_sigma)

    band_high = mean + n_sigma * robust_sigma
    band_low = max(0.0, mean - n_sigma * robust_sigma)
    deviation_sigma = (
        (cur - mean) / robust_sigma if robust_sigma > 0.0 else None
    )

    # above: THE trigger's exact absolute-floor + statistical exceedance.
    above = ats.baseline_exceeds(
        cur, mean, robust_sigma, min_current=min_current, n_sigma=n_sigma
    )
    # below: an unusually-quiet window, but only where the baseline itself
    # clears the floor (a perennially-quiet desk dropping to 0 is not a signal).
    below = (not above) and (cur < band_low) and (mean >= min_current)
    deviation = "above" if above else ("below" if below else "within")

    return BaselineEstimate(
        expected=mean,
        center_median=median,
        robust_sigma=robust_sigma,
        band_low=band_low,
        band_high=band_high,
        n_sigma=n_sigma,
        sample_days=sample_days,
        active_days=active_days,
        insufficient_history=active_days < int(min_active_days),
        deviation=deviation,
        deviation_sigma=deviation_sigma,
    )


def _bucket_at(buckets: Sequence[float], i: int) -> Optional[float]:
    return float(buckets[i]) if 0 <= i < len(buckets) else None


def _rolling_mean(buckets: Sequence[float], days: int) -> Optional[float]:
    """Mean of the ``days`` trailing baseline buckets (buckets[1..days])."""
    window = [float(c) for c in buckets[1 : days + 1]]
    return statistics.fmean(window) if window else None


def lag_features(buckets: Sequence[float]) -> dict[str, Optional[float]]:
    """The CAST lag/rolling-mean feature block over the bucket vector.

    ``buckets[0]`` is the current window; ``buckets[k]`` is k days ago. Lags at
    t-1 / t-7 / t-28 and rolling means over the trailing 7 / 28 baseline days.
    A too-short vector yields ``None`` for the lags it cannot reach (honest,
    never a fabricated 0).
    """
    return {
        "lag_1": _bucket_at(buckets, 1),
        "lag_7": _bucket_at(buckets, 7),
        "lag_28": _bucket_at(buckets, 28),
        "roll_mean_7": _rolling_mean(buckets, 7),
        "roll_mean_28": _rolling_mean(buckets, 28),
    }


# ---------------------------------------------------------------------------
# One stored row
# ---------------------------------------------------------------------------


@dataclass
class DeskBaseline:
    """One (desk, metric) baseline row — the persisted CAST prior."""

    desk_id: str
    metric: str
    geo: list[str]
    baseline_days: int
    n_sigma: float
    expected: float
    center_median: float
    robust_sigma: float
    band_low: float
    band_high: float
    current: float
    deviation: str
    deviation_sigma: Optional[float]
    min_current_floor: float
    sample_days: int
    active_days: int
    insufficient_history: bool
    spillover_current: float
    features: dict[str, Any] = field(default_factory=dict)
    computed_at: Optional[datetime] = None

    @property
    def key(self) -> str:
        return f"{self.desk_id}|{self.metric}"


def build_record(
    desk_id: str,
    geo: Sequence[str],
    metric: str,
    buckets: Sequence[float],
    *,
    n_sigma: float,
    baseline_days: int,
    min_current: float,
    spillover_current: float,
    neighbors: Sequence[str],
    hours_since_last_high_sev: Optional[float],
    now: datetime,
) -> DeskBaseline:
    """Assemble one baseline row from a metric's bucket vector + features."""
    bucket_list = [float(c) for c in buckets]
    current = bucket_list[0] if bucket_list else 0.0
    baseline_counts = bucket_list[1:]
    est = estimate_baseline(
        baseline_counts,
        current,
        n_sigma=n_sigma,
        min_current=min_current,
    )
    feats: dict[str, Any] = dict(lag_features(bucket_list))
    feats["spillover_neighbors"] = list(neighbors)
    feats["neighbor_count"] = len(neighbors)
    feats["hours_since_last_high_sev"] = hours_since_last_high_sev
    feats["nonzero_days"] = est.active_days
    return DeskBaseline(
        desk_id=desk_id,
        metric=metric,
        geo=[str(g) for g in geo],
        baseline_days=baseline_days,
        n_sigma=n_sigma,
        expected=est.expected,
        center_median=est.center_median,
        robust_sigma=est.robust_sigma,
        band_low=est.band_low,
        band_high=est.band_high,
        current=current,
        deviation=est.deviation,
        deviation_sigma=est.deviation_sigma,
        min_current_floor=float(min_current),
        sample_days=est.sample_days,
        active_days=est.active_days,
        insufficient_history=est.insufficient_history,
        spillover_current=float(spillover_current),
        features=feats,
        computed_at=now,
    )


# ---------------------------------------------------------------------------
# SQL — desks + the per-metric bucket vectors (mirrors the P1-3 trigger)
# ---------------------------------------------------------------------------

# The desk set: non-retired target heads tagged g20 or watch (the trigger's
# _DESKS_SQL shape).
_DESKS_SQL = """
    SELECT descriptor_id,
           body -> 'scope' -> 'geo' AS geo
      FROM target_descriptors
     WHERE is_head = TRUE
       AND COALESCE(state, 'active') <> 'retired'
       AND (body -> 'scope' -> 'tags') ?| array['g20', 'watch']
     ORDER BY descriptor_id
     LIMIT $1
"""

# Zero-filled 24h buckets as an ORDERED vector (index 0 = current window,
# 1..N = trailing baseline). Same WHERE clause as the trigger's signal buckets.
_SIGNAL_BUCKET_VECTOR_SQL = """
    WITH hits AS (
        SELECT floor(extract(epoch FROM (now() - fetched_at)) / 86400.0)::int
                 AS bucket
          FROM signals
         WHERE geo && $1::text[]
           AND fetched_at > now() - make_interval(days => $2 + 1)
    ), counts AS (
        SELECT gs.n AS bucket, count(h.bucket) AS c
          FROM generate_series(0, $2::int) gs(n)
          LEFT JOIN hits h ON h.bucket = gs.n
         GROUP BY gs.n
    )
    SELECT array_agg(c ORDER BY bucket)::bigint[] AS buckets FROM counts
"""

_FINDING_BUCKET_VECTOR_SQL = """
    WITH hits AS (
        SELECT floor(extract(epoch FROM (now() - produced_at)) / 86400.0)::int
                 AS bucket
          FROM analyst_outputs
         WHERE kind = 'finding'
           AND target_id = $1
           AND produced_at > now() - make_interval(days => $2 + 1)
           AND (
                 severity IN ('high', 'critical')
                 OR data -> 'tags' ?| ARRAY['severity:high', 'severity:critical']
               )
    ), counts AS (
        SELECT gs.n AS bucket, count(h.bucket) AS c
          FROM generate_series(0, $2::int) gs(n)
          LEFT JOIN hits h ON h.bucket = gs.n
         GROUP BY gs.n
    )
    SELECT array_agg(c ORDER BY bucket)::bigint[] AS buckets FROM counts
"""

_HOURS_SINCE_HIGH_SEV_SQL = """
    SELECT extract(epoch FROM (now() - max(produced_at))) / 3600.0 AS hours
      FROM analyst_outputs
     WHERE kind = 'finding'
       AND target_id = $1
       AND (
             severity IN ('high', 'critical')
             OR data -> 'tags' ?| ARRAY['severity:high', 'severity:critical']
           )
"""


def _coerce_buckets(raw: Any) -> list[float]:
    if not isinstance(raw, (list, tuple)):
        return []
    return [float(x) for x in raw]


# ---------------------------------------------------------------------------
# Store — wholesale upsert + prune (the source_track_record precedent)
# ---------------------------------------------------------------------------

_UPSERT_SQL = """
INSERT INTO desk_baselines (
    desk_id, metric, geo, baseline_days, n_sigma,
    expected, center_median, robust_sigma, band_low, band_high,
    current, deviation, deviation_sigma, min_current_floor,
    sample_days, active_days, insufficient_history,
    spillover_current, features, computed_at
) VALUES (
    $1,$2,$3::jsonb,$4,$5,
    $6,$7,$8,$9,$10,
    $11,$12,$13,$14,
    $15,$16,$17,
    $18,$19::jsonb,$20
)
ON CONFLICT (desk_id, metric) DO UPDATE SET
    geo                  = EXCLUDED.geo,
    baseline_days        = EXCLUDED.baseline_days,
    n_sigma              = EXCLUDED.n_sigma,
    expected             = EXCLUDED.expected,
    center_median        = EXCLUDED.center_median,
    robust_sigma         = EXCLUDED.robust_sigma,
    band_low             = EXCLUDED.band_low,
    band_high            = EXCLUDED.band_high,
    current              = EXCLUDED.current,
    deviation            = EXCLUDED.deviation,
    deviation_sigma      = EXCLUDED.deviation_sigma,
    min_current_floor    = EXCLUDED.min_current_floor,
    sample_days          = EXCLUDED.sample_days,
    active_days          = EXCLUDED.active_days,
    insufficient_history = EXCLUDED.insufficient_history,
    spillover_current    = EXCLUDED.spillover_current,
    features             = EXCLUDED.features,
    computed_at          = EXCLUDED.computed_at
"""

#: Prune (desk, metric) rows no longer in the current set (wholesale refresh).
#: An empty allowlist deletes every row (correct: no current baselines).
_PRUNE_SQL = (
    "DELETE FROM desk_baselines "
    "WHERE desk_id || '|' || metric <> ALL($1::text[])"
)


async def store_baselines(conn: Any, records: Sequence[DeskBaseline]) -> None:
    """Upsert the current baseline set + prune stale (desk,metric) rows in one
    transaction (readers see the old set or the new set, never a half-refresh)."""
    import json

    async with conn.transaction():
        seen: list[str] = []
        for rec in records:
            await conn.execute(
                _UPSERT_SQL,
                rec.desk_id,
                rec.metric,
                json.dumps(rec.geo, separators=(",", ":")),
                int(rec.baseline_days),
                float(rec.n_sigma),
                float(rec.expected),
                float(rec.center_median),
                float(rec.robust_sigma),
                float(rec.band_low),
                float(rec.band_high),
                float(rec.current),
                rec.deviation,
                rec.deviation_sigma,
                float(rec.min_current_floor),
                int(rec.sample_days),
                int(rec.active_days),
                bool(rec.insufficient_history),
                float(rec.spillover_current),
                json.dumps(rec.features, separators=(",", ":"), default=str),
                rec.computed_at,
            )
            seen.append(rec.key)
        await conn.execute(_PRUNE_SQL, seen)


# ---------------------------------------------------------------------------
# Summary finding — honest per-desk distribution (the measurement product)
# ---------------------------------------------------------------------------


def _round(v: float | None, n: int = 3) -> float | None:
    return round(v, n) if v is not None else None


def build_summary(
    records: Sequence[DeskBaseline], *, baseline_days: int, n_sigma: float
) -> FindingPayload:
    """The honest distribution readout across all (desk, metric) baselines.

    NEVER a forecast: it reports the priors + which desks are running above or
    below their own band right now, and how many rest on thin history — the
    measurement product, stated as a statistical baseline.
    """
    by_metric: dict[str, list[DeskBaseline]] = {}
    for rec in records:
        by_metric.setdefault(rec.metric, []).append(rec)

    above = [r for r in records if r.deviation == "above"]
    below = [r for r in records if r.deviation == "below"]
    insufficient = [r for r in records if r.insufficient_history]
    deviating = above + below

    # Top deviating by magnitude of the running sigma (None sorts last).
    def _mag(r: DeskBaseline) -> float:
        return abs(r.deviation_sigma) if r.deviation_sigma is not None else -1.0

    top = sorted(deviating, key=lambda r: (-_mag(r), r.desk_id, r.metric))[
        :_SUMMARY_TOP_N
    ]

    if not records:
        title = "Desk baseline recompute: no g20/watch desks resolved"
    elif deviating:
        title = (
            f"Desk baselines: {len(records)} rows across {len(by_metric)} "
            f"metric(s); {len(above)} above-band, {len(below)} below-band"
        )
    else:
        title = (
            f"Desk baselines: {len(records)} rows recomputed, none deviating "
            f"(all desks within their {baseline_days}d band)"
        )

    body_lines = [
        NO_FORECAST_NOTE,
        "",
        f"desks_metrics={len(records)} baseline_days={baseline_days} "
        f"n_sigma={n_sigma:g}",
        f"above_band={len(above)} below_band={len(below)} "
        f"insufficient_history={len(insufficient)}",
    ]
    for metric in sorted(by_metric):
        rows = by_metric[metric]
        thin = sum(1 for r in rows if r.insufficient_history)
        body_lines.append(
            f"{metric}: n={len(rows)} "
            f"above={sum(1 for r in rows if r.deviation == 'above')} "
            f"below={sum(1 for r in rows if r.deviation == 'below')} "
            f"thin_history={thin}"
        )
    if top:
        body_lines.append("")
        body_lines.append("most-deviating (current vs expected±band):")
        for r in top:
            sig = (
                f"{r.deviation_sigma:+.1f}σ"
                if r.deviation_sigma is not None
                else "σ=n/a"
            )
            body_lines.append(
                f"  [{r.deviation}] {r.desk_id} {r.metric}: "
                f"current={r.current:.0f} expected={r.expected:.1f} "
                f"band=[{r.band_low:.1f},{r.band_high:.1f}] {sig}"
                + ("  (thin history)" if r.insufficient_history else "")
            )

    return FindingPayload(
        title=title[:2048],
        body="\n".join(body_lines)[:65536],
        confidence=1.0,
        evidence=[],
        tags=["deterministic", SUB_HANDLER_NAME],
        data={
            "sub_handler": SUB_HANDLER_NAME,
            "not_a_forecast": True,
            "honesty_note": NO_FORECAST_NOTE,
            "baseline_days": baseline_days,
            "n_sigma": n_sigma,
            "desks_metrics": len(records),
            "above_band": len(above),
            "below_band": len(below),
            "insufficient_history": len(insufficient),
            "by_metric": {
                metric: {
                    "n": len(rows),
                    "above": sum(1 for r in rows if r.deviation == "above"),
                    "below": sum(1 for r in rows if r.deviation == "below"),
                    "thin_history": sum(
                        1 for r in rows if r.insufficient_history
                    ),
                }
                for metric, rows in by_metric.items()
            },
            "top_deviating": [
                {
                    "desk_id": r.desk_id,
                    "metric": r.metric,
                    "deviation": r.deviation,
                    "current": r.current,
                    "expected": _round(r.expected),
                    "band_low": _round(r.band_low),
                    "band_high": _round(r.band_high),
                    "deviation_sigma": _round(r.deviation_sigma, 2),
                    "insufficient_history": r.insufficient_history,
                }
                for r in top
            ],
        },
    )


# ---------------------------------------------------------------------------
# Compute — one global recompute over the desk set
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def compute_desk_baselines(
    conn: Any,
    *,
    baseline_days: int,
    n_sigma: float,
    now: datetime,
) -> list[DeskBaseline]:
    """Compute every (desk, metric) baseline row (two passes for spillover)."""
    desks = await conn.fetch(_DESKS_SQL, _MAX_DESKS)

    # First pass: desk geo + per-desk bucket vectors + current signal volume.
    desk_geo: dict[str, list[str]] = {}
    signal_buckets: dict[str, Optional[list[float]]] = {}
    finding_buckets: dict[str, list[float]] = {}
    hours_since: dict[str, Optional[float]] = {}
    current_signal_by_desk: dict[str, float] = {}
    iso2_to_desk: dict[str, str] = {}

    for desk_row in desks:
        desk = str(desk_row["descriptor_id"])
        geo_raw = ats._parse_jsonish(desk_row["geo"])
        geo = [str(g) for g in geo_raw] if isinstance(geo_raw, list) else []
        desk_geo[desk] = geo
        for code in geo:
            iso2_to_desk.setdefault(code.strip().upper(), desk)

        if geo:
            sig_row = await conn.fetchrow(
                _SIGNAL_BUCKET_VECTOR_SQL, geo, int(baseline_days)
            )
            sig_vec = _coerce_buckets(sig_row["buckets"] if sig_row else None)
            signal_buckets[desk] = sig_vec
            current_signal_by_desk[desk] = sig_vec[0] if sig_vec else 0.0
        else:
            signal_buckets[desk] = None
            current_signal_by_desk[desk] = 0.0

        fnd_row = await conn.fetchrow(
            _FINDING_BUCKET_VECTOR_SQL, desk, int(baseline_days)
        )
        finding_buckets[desk] = _coerce_buckets(
            fnd_row["buckets"] if fnd_row else None
        )
        hrs_row = await conn.fetchrow(_HOURS_SINCE_HIGH_SEV_SQL, desk)
        hours_since[desk] = (
            float(hrs_row["hours"])
            if hrs_row is not None and hrs_row["hours"] is not None
            else None
        )

    # Second pass: assemble rows (spillover needs the full current-signal map).
    records: list[DeskBaseline] = []
    for desk in sorted(desk_geo):
        geo = desk_geo[desk]
        neighbors = neighbor_desks(geo, iso2_to_desk, self_desk=desk)
        spillover = sum(current_signal_by_desk.get(n, 0.0) for n in neighbors)

        sig_vec = signal_buckets.get(desk)
        if sig_vec is not None:
            records.append(
                build_record(
                    desk,
                    geo,
                    METRIC_SIGNAL_VOLUME,
                    sig_vec,
                    n_sigma=n_sigma,
                    baseline_days=baseline_days,
                    min_current=float(MIN_CURRENT_SIGNALS),
                    spillover_current=spillover,
                    neighbors=neighbors,
                    hours_since_last_high_sev=hours_since.get(desk),
                    now=now,
                )
            )
        records.append(
            build_record(
                desk,
                geo,
                METRIC_HIGH_SEV_FINDINGS,
                finding_buckets.get(desk, []),
                n_sigma=n_sigma,
                baseline_days=baseline_days,
                min_current=float(MIN_CURRENT_FINDINGS),
                spillover_current=spillover,
                neighbors=neighbors,
                hours_since_last_high_sev=hours_since.get(desk),
                now=now,
            )
        )
    return records


# ---------------------------------------------------------------------------
# Public handler entry point
# ---------------------------------------------------------------------------


async def handle(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: Any | None,
) -> AnalystMethodResult:
    """Sub-handler entry point — one global desk-baseline recompute + store.

    REFUSES LOUD on a missing pool (the sibling deterministic-META contract): a
    baseline that cannot read the substrate must error visibly, never report a
    quiet zero-desk run.
    """
    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    if pool is None:
        raise RuntimeError(
            "desk_baseline requires a live deps.pg_pool — refusing to report a "
            "zero-desk baseline without reading the substrate"
        )

    raw_run_id = options.get("run_id")
    try:
        _ = UUID(str(raw_run_id)) if raw_run_id else uuid4()
    except (ValueError, TypeError):
        _ = uuid4()

    baseline_days = max(2, int(options.get("baseline_days", DEFAULT_BASELINE_DAYS)))
    n_sigma = float(options.get("baseline_sigma", DEFAULT_N_SIGMA))
    now = _now()

    async with pool.acquire() as conn:
        records = await compute_desk_baselines(
            conn, baseline_days=baseline_days, n_sigma=n_sigma, now=now
        )
        await store_baselines(conn, records)

    above = sum(1 for r in records if r.deviation == "above")
    below = sum(1 for r in records if r.deviation == "below")
    if records:
        logger.info(
            "desk_baseline.done rows=%d above=%d below=%d insufficient=%d",
            len(records),
            above,
            below,
            sum(1 for r in records if r.insufficient_history),
        )

    finding = build_summary(
        records, baseline_days=baseline_days, n_sigma=n_sigma
    )
    return AnalystMethodResult(
        finding=finding,
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
    )


__all__ = [
    "SUB_HANDLER_NAME",
    "METRIC_SIGNAL_VOLUME",
    "METRIC_HIGH_SEV_FINDINGS",
    "DEFAULT_BASELINE_DAYS",
    "DEFAULT_N_SIGMA",
    "MIN_CURRENT_SIGNALS",
    "MIN_CURRENT_FINDINGS",
    "MIN_ACTIVE_DAYS",
    "NO_FORECAST_NOTE",
    "LAND_ADJACENCY",
    "build_adjacency",
    "neighbor_desks",
    "BaselineEstimate",
    "estimate_baseline",
    "lag_features",
    "DeskBaseline",
    "build_record",
    "store_baselines",
    "build_summary",
    "compute_desk_baselines",
    "handle",
]
