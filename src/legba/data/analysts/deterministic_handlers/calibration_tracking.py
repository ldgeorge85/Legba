# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``calibration_tracking`` sub-handler — L-006 sub-split D.

Tracks analyst confidence-vs-outcome quality over time. Pure SQL/numpy
aggregation; no LLM.

Inputs are resolved claim rows of shape::

    {
        "analyst_id": str,
        "claim_id": str,         # hypothesis or prediction id
        "claim_kind": "hypothesis" | "prediction",
        "claimed_confidence": float,   # 0..1
        "outcome": 0 | 1,        # 1 = resolved as predicted, 0 = refuted
        "resolved_at": iso8601 str,
    }

For ACH hypotheses the substrate pull reads the EXOGENOUS ``resolved_outcome``
column (migration 0038) — an outcome stamped against SUBSEQUENT facts or an
operator label — NOT the hypothesis ``status`` (which is auto-transitioned from
its own ``evidence_balance``). Scoring ``status`` against an ``evidence_balance``
-derived confidence is a CIRCULAR Brier; reading ``resolved_outcome`` grades the
claim against what the world subsequently showed (the real calibration signal).

The handler computes:

  * **Brier score** = mean((p - o)^2). 0 perfect, 0.25 baseline-50%.
  * **Reliability bins** — bucket claims by claimed_confidence (10 bins),
    report (mean_claimed, mean_outcome, count) per bin so a reliability
    curve can be plotted.
  * **Per-analyst Brier** — same calc but grouped by analyst_id for
    cross-analyst comparison.
  * **Rolling Brier** by week — last 12 weeks if data permits.
  * **Drift signal** — z-score of latest week's Brier against the
    trailing 12-week distribution.

Output ``data`` keys:
    brier               float | None
    sample_size         int
    reliability_bins    [{lo, hi, mean_claimed, mean_outcome, count}]
    per_analyst         {analyst_id: {brier, sample_size}}
    rolling_brier       [{week_start, brier, sample_size}]
    drift_z             float | None
    drift_alert         bool      # True if |drift_z| > drift_threshold
    drift_threshold     float
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

import numpy as np

from ...provenance.models import FindingPayload
from ....runtime.analyst_method import AnalystMethodResult
from . import forecast_acute

logger = logging.getLogger(__name__)

_DEFAULT_BIN_COUNT = 10
_DEFAULT_ROLLING_WEEKS = 12
_DEFAULT_DRIFT_THRESHOLD = 2.0

# R1-T1.3 (#92) — the pre-registered acute-binary forecast pilot reports its
# segregated Brier only once it has at least this many EXOGENOUSLY-resolved
# (country, week) calls, so the first number isn't a small-sample artifact.
# Below it the finding says "accumulating (n=k/N)". The pilot's number lives in
# its OWN key (`brier_forecast_acute`) and is NEVER pooled into the headline.
_FORECAST_ACUTE_MIN_SAMPLE = 30

# DQ-H2: the headline Brier is only meaningful over EXOGENOUS resolutions
# (the world graded the claim), not self-consistency ones (the system graded
# itself). Below this many exogenous rows we report "insufficient exogenous
# sample" rather than a number that looks calibrated but isn't.
_MIN_EXOGENOUS_FOR_BRIER = 5
# The horizon-end prediction resolver's provenance label — EXOGENOUS (forecast
# CI vs the actual realized event rate).
_FORECAST_RESOLVER_SOURCE = "forecast_vs_actual"


# ---------------------------------------------------------------------------
# Core metrics
# ---------------------------------------------------------------------------


def _brier(rows: list[dict[str, Any]]) -> float | None:
    """Brier score over rows. Returns None if no rows."""
    if not rows:
        return None
    p = np.asarray([float(r["claimed_confidence"]) for r in rows], dtype=float)
    o = np.asarray([float(r["outcome"]) for r in rows], dtype=float)
    return float(np.mean((p - o) ** 2))


def _reliability_bins(
    rows: list[dict[str, Any]],
    *,
    bin_count: int,
) -> list[dict[str, Any]]:
    """Bucket claims by claimed_confidence and report per-bin stats.

    Bins are equal-width over [0, 1]. Each output dict reports
    ``mean_claimed`` (avg p in the bin), ``mean_outcome`` (avg o in the
    bin — this is the "empirical confidence"), and ``count``.
    """
    if not rows:
        return []
    p = np.asarray([float(r["claimed_confidence"]) for r in rows], dtype=float)
    o = np.asarray([float(r["outcome"]) for r in rows], dtype=float)
    edges = np.linspace(0.0, 1.0, bin_count + 1)
    bins: list[dict[str, Any]] = []
    for i in range(bin_count):
        lo, hi = float(edges[i]), float(edges[i + 1])
        # Include the right edge in the last bin so p=1.0 doesn't get dropped.
        if i == bin_count - 1:
            mask = (p >= lo) & (p <= hi)
        else:
            mask = (p >= lo) & (p < hi)
        count = int(mask.sum())
        if count == 0:
            bins.append({
                "lo": lo, "hi": hi,
                "mean_claimed": None, "mean_outcome": None, "count": 0,
            })
            continue
        bins.append({
            "lo": lo, "hi": hi,
            "mean_claimed": float(p[mask].mean()),
            "mean_outcome": float(o[mask].mean()),
            "count": count,
        })
    return bins


def _per_analyst_brier(
    rows: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Brier score and sample size per analyst_id."""
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        aid = str(r.get("analyst_id") or "_unknown")
        grouped[aid].append(r)
    return {
        aid: {
            "brier": _brier(group),
            "sample_size": len(group),
        }
        for aid, group in grouped.items()
    }


# ---------------------------------------------------------------------------
# Rolling Brier + drift
# ---------------------------------------------------------------------------


def _parse_resolved_at(raw: Any) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    try:
        # Tolerate trailing 'Z' that fromisoformat <3.11 chokes on.
        s = str(raw)
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _week_start(dt: datetime) -> datetime:
    """ISO-week Monday (UTC) at 00:00 for the input datetime."""
    d = dt.astimezone(timezone.utc)
    monday = d - timedelta(days=d.weekday())
    return monday.replace(hour=0, minute=0, second=0, microsecond=0)


def _rolling_brier(
    rows: list[dict[str, Any]],
    *,
    max_weeks: int,
) -> list[dict[str, Any]]:
    """Compute weekly Brier scores. Returns up to ``max_weeks`` most
    recent buckets (oldest first).
    """
    parsed: list[tuple[datetime, dict[str, Any]]] = []
    for r in rows:
        dt = _parse_resolved_at(r.get("resolved_at"))
        if dt is None:
            continue
        parsed.append((dt, r))
    if not parsed:
        return []
    buckets: dict[datetime, list[dict[str, Any]]] = defaultdict(list)
    for dt, r in parsed:
        buckets[_week_start(dt)].append(r)
    weeks = sorted(buckets.keys())[-max_weeks:]
    return [
        {
            "week_start": w.isoformat(),
            "brier": _brier(buckets[w]),
            "sample_size": len(buckets[w]),
        }
        for w in weeks
    ]


def _drift_z(
    rolling: list[dict[str, Any]],
) -> float | None:
    """Z-score of the latest week's Brier vs the trailing distribution.

    Returns None if fewer than 3 weeks of data, or if all weeks have None
    Brier (no resolved claims).
    """
    series = [w["brier"] for w in rolling if w["brier"] is not None]
    if len(series) < 3:
        return None
    latest = series[-1]
    baseline = np.asarray(series[:-1], dtype=float)
    mean = float(baseline.mean())
    std = float(baseline.std(ddof=1)) if baseline.size > 1 else 0.0
    if std == 0.0:
        return 0.0
    return float((latest - mean) / std)


# ---------------------------------------------------------------------------
# Live-substrate pull (best-effort)
# ---------------------------------------------------------------------------


async def _resolve_region_geo(conn: Any, region: str) -> list[str]:
    """Resolve a prediction's ``region`` (the target descriptor id, e.g.
    ``country_g20_us``) to the geo codes signals are tagged with — prefer the
    target's declared ``scope.geo``, fall back to the ISO2 suffix."""
    try:
        trow = await conn.fetchrow(
            "SELECT body FROM target_descriptors "
            "WHERE descriptor_id = $1 AND is_head = TRUE",
            region,
        )
        if trow and trow["body"]:
            tbody = trow["body"]
            if isinstance(tbody, str):
                tbody = json.loads(tbody)
            scope = (tbody.get("scope") or {}) if isinstance(tbody, dict) else {}
            geo = [g for g in (scope.get("geo") or []) if g]
            if geo:
                return geo
    except Exception:  # noqa: BLE001 — fall through to the suffix heuristic
        pass
    suffix = region.rsplit("_", 1)[-1].upper() if region else ""
    return [suffix] if len(suffix) == 2 and suffix.isalpha() else []


async def _grade_one_prediction(
    conn: Any, data: Mapping[str, Any], geo_cache: dict[str, list[str]],
) -> dict[str, Any] | None:
    """Grade ONE past-horizon forecast: did the predicted CI contain the
    ACTUAL realized event rate over its horizon window? Returns
    ``{"hit": bool, "actual": float}`` or ``None`` if it can't be graded
    (missing fields, unresolvable geo, no signal data)."""
    try:
        lo = float(data.get("ci_lower"))
        hi = float(data.get("ci_upper"))
        hdays = max(1, int(float(data.get("horizon_days") or 7)))
    except (TypeError, ValueError):
        return None
    region = str(data.get("region") or "")
    # Parse horizon_end → an aware datetime (asyncpg binds timestamptz params as
    # datetime objects, NOT strings). Accepts bare dates ('2026-06-22') + ISO.
    raw_end = data.get("horizon_end")
    if not raw_end or not region:
        return None
    if isinstance(raw_end, datetime):
        horizon_end = raw_end if raw_end.tzinfo else raw_end.replace(tzinfo=timezone.utc)
    else:
        try:
            horizon_end = datetime.fromisoformat(str(raw_end).replace("Z", "+00:00"))
        except ValueError:
            return None
        if horizon_end.tzinfo is None:
            horizon_end = horizon_end.replace(tzinfo=timezone.utc)
    geo = geo_cache.get(region)
    if geo is None:
        geo = await _resolve_region_geo(conn, region)
        geo_cache[region] = geo
    if not geo:
        return None
    try:
        actrow = await conn.fetchrow(
            """
            SELECT count(*)::float AS cnt
            FROM signals
            WHERE geo && $1::text[]
              AND fetched_at >  ($2::timestamptz - make_interval(days => $3::int))
              AND fetched_at <= $2::timestamptz
            """,
            geo, horizon_end, hdays,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("calibration_tracking.grade_query_failed err=%s", exc)
        return None
    if actrow is None:
        return None
    actual = float(actrow["cnt"]) / float(hdays)
    return {"hit": bool(lo <= actual <= hi), "actual": actual}


async def _resolve_open_predictions(deps: Any, options: Mapping[str, Any]) -> int:
    """Horizon-end resolver — the EXOGENOUS calibration-data generator (DQ-H2).

    Grades every open prediction whose horizon has passed by comparing its
    forecast CI to the realized event rate, then stamps
    ``data.resolved_outcome`` + ``resolved_by='forecast_vs_actual'`` back onto
    the prediction row. Without this, predictions stay 100% ``open`` forever
    and the calibration loop has NO exogenous input — the whole reason the
    headline Brier was a self-consistency artifact. Best-effort; returns the
    count resolved. The bounded LIMIT keeps a backlog from blocking a tick.
    """
    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    if pool is None:
        return 0
    lookback_days = int(options.get("lookback_days", 365))
    resolved = 0
    try:
        async with pool.acquire() as conn:
            open_rows = await conn.fetch(
                """
                SELECT id::text AS id, confidence, data
                FROM analyst_outputs
                WHERE kind = 'prediction'
                  AND (data->>'resolved_outcome') IS NULL
                  AND (data->>'horizon_end') IS NOT NULL
                  AND (data->>'horizon_end')::timestamptz < NOW()
                  AND produced_at > NOW() - make_interval(days => $1)
                ORDER BY produced_at
                LIMIT 500
                """,
                lookback_days,
            )
            geo_cache: dict[str, list[str]] = {}
            for row in open_rows:
                data = row["data"] or {}
                if isinstance(data, str):
                    try:
                        data = json.loads(data)
                    except Exception:
                        continue
                graded = await _grade_one_prediction(conn, data, geo_cache)
                if graded is None:
                    continue
                await conn.execute(
                    """
                    UPDATE analyst_outputs
                    SET data = data || jsonb_build_object(
                            'status', $2::text,
                            'resolved_outcome', $3::int,
                            'actual_value', $4::double precision,
                            'resolved_by', $5::text,
                            'resolved_at', $6::text
                        )
                    WHERE id = $1::uuid
                    """,
                    row["id"],
                    "resolved" if graded["hit"] else "refuted",
                    1 if graded["hit"] else 0,
                    float(graded["actual"]),
                    _FORECAST_RESOLVER_SOURCE,
                    datetime.now(timezone.utc).isoformat(),
                )
                resolved += 1
    except Exception as exc:  # noqa: BLE001 — never break the calibration tick
        logger.warning("calibration_tracking.resolve_predictions_failed err=%s", exc)
    if resolved:
        logger.info(
            "calibration_tracking.predictions_resolved n=%d source=%s",
            resolved, _FORECAST_RESOLVER_SOURCE,
        )
    return resolved


async def _pull_resolved_claims(
    deps: Any,
    options: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Pull resolved hypotheses + predictions from substrate.

    Best-effort; failure → ``[]``. Joins each claim with its claimed
    confidence at creation time and current resolution status.
    """
    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    if pool is None:
        return []
    lookback_days = int(options.get("lookback_days", 365))
    pulled: list[dict[str, Any]] = []

    # The two sources are pulled in INDEPENDENT try-blocks so a missing/empty
    # source does NOT starve the other.

    # Predictions live in analyst_outputs (kind='prediction'); the standalone
    # ``predictions`` table was DROPPED by migration 0024, so the old
    # ``FROM predictions`` query silently returned nothing and predictions never
    # reached calibration (DQ-H1(c)/H2). A prediction becomes calibration data
    # only once the horizon-end resolver (_resolve_open_predictions) stamps
    # ``data.resolved_outcome`` + ``resolved_by='forecast_vs_actual'`` (EXOGENOUS
    # — the CI was graded against the realized event rate).
    try:
        async with pool.acquire() as conn:
            pred_rows = await conn.fetch(
                """
                SELECT
                    analyst_id, id::text AS claim_id,
                    'prediction' AS claim_kind,
                    confidence AS claimed_confidence,
                    (data->>'resolved_outcome')::int AS outcome,
                    COALESCE(data->>'resolved_by', $2) AS resolved_by,
                    data->>'resolved_at' AS resolved_at
                FROM analyst_outputs
                WHERE kind = 'prediction'
                  AND data->>'resolved_outcome' IS NOT NULL
                  AND produced_at > NOW() - make_interval(days => $1)
                """,
                lookback_days, _FORECAST_RESOLVER_SOURCE,
            )
        for row in pred_rows:
            if row["outcome"] is None:
                continue
            pulled.append({
                "analyst_id": row["analyst_id"],
                "claim_id": row["claim_id"],
                "claim_kind": row["claim_kind"],
                "claimed_confidence": float(row["claimed_confidence"] or 0.5),
                "outcome": int(row["outcome"]),
                "resolved_at": row["resolved_at"],
                "resolved_by": row["resolved_by"],
            })
    except Exception as exc:
        logger.warning("calibration_tracking.pull_predictions_failed err=%s", exc)

    # Hypotheses (the ACH competing_hypotheses producer): the OUTCOME is the
    # EXOGENOUS `resolved_outcome` column (migration 0038) — 1 = the thesis came
    # TRUE, 0 = it did not — stamped by a resolver that reads SUBSEQUENT facts or
    # by an operator label. We deliberately do NOT read `status` here: `status`
    # is auto-transitioned FROM `evidence_balance`, and the claimed confidence is
    # derived FROM `evidence_balance`, so scoring `status` against that confidence
    # is a CIRCULAR Brier (the system graded against its own evidence count —
    # incapable of detecting miscalibration). Reading `resolved_outcome` grades
    # the claim against what the WORLD subsequently showed, which is the whole
    # point of a calibration score. Rows with `resolved_outcome IS NULL`
    # (unresolved) are absent from the sample until a resolver/operator stamps
    # them. The claimed confidence is still derived from |evidence_balance| at
    # claim time — that is the analyst's CLAIM, which is exactly what calibration
    # measures against the exogenous outcome.
    try:
        async with pool.acquire() as conn:
            hyp_rows = await conn.fetch(
                """
                SELECT
                    analyst_id, id::text AS claim_id,
                    'hypothesis' AS claim_kind,
                    evidence_balance,
                    resolved_outcome AS outcome,
                    resolved_at,
                    resolved_by
                FROM hypotheses
                WHERE resolved_outcome IS NOT NULL
                  AND produced_at > NOW() - make_interval(days => $1)
                """,
                lookback_days,
            )
        for row in hyp_rows:
            if row["outcome"] is None:
                continue
            bal = abs(int(row["evidence_balance"] or 0))
            claimed = min(0.95, 0.5 + 0.15 * bal)
            pulled.append({
                "analyst_id": row["analyst_id"],
                "claim_id": row["claim_id"],
                "claim_kind": row["claim_kind"],
                "claimed_confidence": float(claimed),
                "outcome": int(row["outcome"]),
                "resolved_at": (
                    row["resolved_at"].isoformat() if row["resolved_at"] else None
                ),
                # Resolution-source provenance — the calibration HONESTY axis.
                # 'status_transition' is SELF-CONSISTENCY (the prediction and the
                # resolution share the same evidence_balance); 'subsequent_facts'
                # / 'operator:*' are EXOGENOUS. Carried through so the handler can
                # report the breakdown + flag a self-consistency-only Brier.
                "resolved_by": row["resolved_by"],
            })
    except Exception as exc:
        logger.warning("calibration_tracking.pull_hypotheses_failed err=%s", exc)

    return pulled


# Resolution-source labels that are SELF-CONSISTENCY (not exogenous ground
# truth) — see competing_hypotheses._resolve_hypotheses_by_status_transition.
_SELF_CONSISTENCY_SOURCES: frozenset[str] = frozenset({"status_transition"})


def _is_exogenous(row: Mapping[str, Any]) -> bool:
    """A resolution is EXOGENOUS when the WORLD graded the claim (subsequent
    facts, an operator label, or the forecast-vs-actual resolver), as opposed
    to the system grading itself (status_transition). An unlabeled / unknown
    source is treated conservatively as NON-exogenous so it can never inflate
    the honest headline Brier."""
    rb = str(row.get("resolved_by") or "").strip()
    if not rb or rb == "unknown":
        return False
    return rb not in _SELF_CONSISTENCY_SOURCES


def _resolution_source_breakdown(
    rows: list[dict[str, Any]],
) -> tuple[dict[str, int], bool]:
    """Count resolved rows by ``resolved_by`` and decide the self-consistency
    flag.

    Returns ``(breakdown, self_consistency_only)``. ``self_consistency_only`` is
    ``True`` when the sample is non-empty and EVERY resolved row came from a
    self-consistency source (no exogenous outcome present) — the signal the
    finding uses to mark the Brier as a self-consistency check rather than a
    calibration against reality. A row with no ``resolved_by`` (e.g. a
    prediction, or a pre-FIX hypothesis) counts as ``'unknown'`` and is treated
    conservatively as NON-self-consistency (so it does not let an unlabelled
    sample masquerade as exogenous, nor force the self-consistency flag).
    """
    breakdown: dict[str, int] = {}
    for r in rows:
        src = r.get("resolved_by")
        key = str(src) if src else "unknown"
        breakdown[key] = breakdown.get(key, 0) + 1
    if not rows:
        return breakdown, False
    self_consistency_only = all(
        (str(r.get("resolved_by") or "") in _SELF_CONSISTENCY_SOURCES)
        for r in rows
    )
    return breakdown, self_consistency_only


# ---------------------------------------------------------------------------
# R1-T1.3 (#92) — segregated acute-forecast pilot metrics
# ---------------------------------------------------------------------------


def _forecast_acute_metrics(
    acute_rows: list[dict[str, Any]],
    *,
    min_sample: int,
) -> dict[str, Any]:
    """Brier + Brier-skill-score over the pre-registered acute-binary pilot,
    kept STRICTLY separate from the headline calibration Brier.

    ``brier_skill_score`` (BSS) = 1 - brier_model / brier_climatology. The model
    is ``p`` (recent-rate Poisson tail). The climatology reference is the TEXTBOOK
    BSS baseline — a constant forecast of the REALIZED sample base rate
    ``ō = mean(o)`` — NOT the issue-time per-row ``p_base``. Using the realized
    base rate is deliberate: a per-row ``p_base`` estimated from a thin / mixed-
    coverage record (e.g. USGS's rolling 30-day feed vs NASA's multi-year one)
    can be spuriously LOW, which would hand the model unearned skill. The
    constant-base-rate reference cannot be under-estimated and refuses to credit
    skill on a degenerate sample (all-0 or all-1 → climatology Brier 0 → BSS
    None). The project earns the word "forecast" only when BSS > 0 on the pilot.
    Below ``min_sample`` resolved calls the headline number is withheld
    (``ready=False``, status ``accumulating``).
    """
    p_model: list[float] = []
    p_base: list[float] = []
    o: list[float] = []
    for r in acute_rows:
        try:
            pm = float(r["claimed_confidence"])
            pb = float(r.get("p_base", 0.0))
            out = int(r["outcome"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (0.0 <= pm <= 1.0) or out not in (0, 1):
            continue
        p_model.append(pm)
        p_base.append(min(1.0, max(0.0, pb)))
        o.append(float(out))

    n = len(o)
    if n == 0:
        return {
            "brier_forecast_acute": None,
            "brier_climatology": None,
            "brier_skill_score": None,
            "forecast_acute_sample_size": 0,
            "forecast_acute_ready": False,
            "forecast_acute_status": "no resolved pilot calls yet",
            "forecast_acute_base_rate": None,
            "forecast_acute_p_base_mean": None,
        }
    pm_arr = np.asarray(p_model, dtype=float)
    pb_arr = np.asarray(p_base, dtype=float)
    o_arr = np.asarray(o, dtype=float)
    obar = float(o_arr.mean())  # realized sample base rate = the climatology forecast
    brier_model = float(np.mean((pm_arr - o_arr) ** 2))
    brier_base = float(np.mean((obar - o_arr) ** 2))  # constant-base-rate reference
    # BSS is only defined when climatology has non-trivial error to improve on;
    # a degenerate (all-same-outcome) sample yields brier_base 0 → BSS None.
    bss = (1.0 - brier_model / brier_base) if brier_base > 1e-9 else None

    # DEGENERACY GUARD (honesty): a "forecast" that only ever emits near-0/near-1
    # certainties is not probabilistic forecasting — at the country-week-binary
    # granularity these hazard catalogs are dominated by STATIC GEOGRAPHY (a
    # seismic country is certain, a non-seismic one impossible), so a positive BSS
    # would reflect knowing the map, not anticipating the future. We measure the
    # share of GENUINELY UNCERTAIN calls and refuse the "earned" claim below it.
    probabilistic_share = float(np.mean((pm_arr > 0.05) & (pm_arr < 0.95)))
    degenerate = probabilistic_share < 0.2
    ready = n >= min_sample
    if degenerate:
        status = (
            f"degenerate — geography-dominated, not probabilistic forecasting "
            f"(prob_share={probabilistic_share:.0%}, n={n}); skill claim withheld"
        )
    elif ready:
        status = "ready"
    else:
        status = (
            f"accumulating (n={n}/{min_sample}) — pilot / short-record / hazard-only"
        )
    return {
        # The pilot headline is reported only when the sample is large enough;
        # below that the raw value lives under the diagnostic key only.
        "brier_forecast_acute": brier_model if ready else None,
        "brier_forecast_acute_raw": brier_model,
        "brier_climatology": brier_base,
        "brier_skill_score": bss,
        "forecast_acute_sample_size": n,
        "forecast_acute_ready": ready,
        "forecast_acute_degenerate": degenerate,
        "forecast_acute_probabilistic_share": probabilistic_share,
        "forecast_acute_status": status,
        "forecast_acute_base_rate": obar,
        # Diagnostic only: the mean issue-time per-country climatology estimate
        # (NOT the BSS reference — see the docstring on why ō is used instead).
        "forecast_acute_p_base_mean": float(pb_arr.mean()),
    }


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def _validate_rows(
    raw: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Drop rows that lack the minimum required fields. Returns (kept, warnings)."""
    kept: list[dict[str, Any]] = []
    warnings: list[str] = []
    drops = 0
    for r in raw:
        try:
            conf = float(r["claimed_confidence"])
            out = int(r["outcome"])
        except (KeyError, TypeError, ValueError):
            drops += 1
            continue
        if not (0.0 <= conf <= 1.0):
            drops += 1
            continue
        if out not in (0, 1):
            drops += 1
            continue
        kept.append({
            "analyst_id": str(r.get("analyst_id") or "_unknown"),
            "claim_id": str(r.get("claim_id") or ""),
            "claim_kind": str(r.get("claim_kind") or "unknown"),
            "claimed_confidence": conf,
            "outcome": out,
            "resolved_at": r.get("resolved_at"),
            "resolved_by": r.get("resolved_by"),
        })
    if drops:
        warnings.append(
            f"calibration_tracking.dropped_invalid count={drops}"
        )
    return kept, warnings


# ---------------------------------------------------------------------------
# Finding assembly
# ---------------------------------------------------------------------------


def _build_finding(
    *,
    brier: float | None,
    sample_size: int,
    reliability_bins: list[dict[str, Any]],
    per_analyst: dict[str, dict[str, Any]],
    rolling: list[dict[str, Any]],
    drift_z: float | None,
    drift_threshold: float,
    resolution_sources: dict[str, int],
    self_consistency_only: bool,
    brier_exogenous: float | None,
    brier_self_consistency: float | None,
    brier_pooled: float | None,
    exogenous_sample_size: int,
    self_consistency_fraction: float,
    insufficient_exogenous: bool,
    forecast_acute: dict[str, Any],
    warnings: list[str],
    target_id: str | None,
) -> FindingPayload:
    drift_alert = drift_z is not None and abs(drift_z) > drift_threshold
    # HONEST HEADLINE (DQ-H2): `brier` is the EXOGENOUS-only Brier — the only
    # number that measures calibration against reality. When too few exogenous
    # resolutions exist we report NO headline number (and say so) rather than
    # surfacing the pooled/self-consistency Brier as if it were calibration.
    if brier is not None:
        head = (
            f"Calibration: exogenous brier={brier:.4f} "
            f"(n_exo={exogenous_sample_size}/{sample_size}, "
            f"self_consistency={self_consistency_fraction:.0%})"
        )
    elif sample_size > 0:
        head = (
            f"Calibration: INSUFFICIENT exogenous sample "
            f"(n_exo={exogenous_sample_size}/{sample_size}, "
            f"self_consistency={self_consistency_fraction:.0%}; "
            f"self-consistency brier={brier_self_consistency})"
        )
    else:
        head = "Calibration: n=0 (no resolved claims)"
    title = f"{head} for {target_id}" if target_id else head
    body = (
        f"sample_size={sample_size}\n"
        f"headline_brier_exogenous_only={brier}\n"
        f"exogenous_sample_size={exogenous_sample_size}\n"
        f"brier_self_consistency={brier_self_consistency}\n"
        f"self_consistency_fraction={self_consistency_fraction:.4f}\n"
        f"insufficient_exogenous={insufficient_exogenous}\n"
        f"resolution_sources={resolution_sources}\n"
        f"per_analyst={len(per_analyst)} analysts\n"
        f"rolling_weeks={len(rolling)}\n"
        f"drift_z={drift_z} alert={drift_alert}\n"
        # R1-T1.3 (#92): the pre-registered acute-binary forecast pilot —
        # SEGREGATED from the headline, reported with its climatology skill score.
        f"forecast_acute_status={forecast_acute.get('forecast_acute_status')}\n"
        f"forecast_acute_n={forecast_acute.get('forecast_acute_sample_size')}\n"
        f"brier_forecast_acute={forecast_acute.get('brier_forecast_acute')}\n"
        f"brier_climatology={forecast_acute.get('brier_climatology')}\n"
        f"brier_skill_score={forecast_acute.get('brier_skill_score')}\n"
    )
    tags = ["deterministic", "calibration_tracking"]
    if drift_alert:
        tags.append("calibration_drift_alert")
    # Earned-forecast tag: ONLY when the pilot is at-sample, beats climatology,
    # AND is non-degenerate (genuinely probabilistic, not geography-dominated).
    if (
        forecast_acute.get("forecast_acute_ready")
        and not forecast_acute.get("forecast_acute_degenerate")
        and (forecast_acute.get("brier_skill_score") or 0.0) > 0.0
    ):
        tags.append("forecast_skill_positive")
    if forecast_acute.get("forecast_acute_degenerate") and (
        forecast_acute.get("forecast_acute_sample_size") or 0
    ) > 0:
        tags.append("forecast_pilot_degenerate")
    if insufficient_exogenous and sample_size > 0:
        # HONESTY tag: not enough EXOGENOUS resolutions to claim calibration.
        tags.append("brier_insufficient_exogenous")
    if self_consistency_only and sample_size > 0:
        tags.append("brier_self_consistency_only")
    return FindingPayload(
        title=title[:2048],
        body=body[:65536],
        confidence=1.0,
        evidence=[],
        tags=tags,
        data={
            "sub_handler": "calibration_tracking",
            # `brier` is the HONEST headline = exogenous-only (None when
            # insufficient exogenous sample). The pooled + self-consistency
            # Briers are kept as DIAGNOSTICS, never as the headline.
            "brier": brier,
            "brier_exogenous": brier_exogenous,
            "brier_self_consistency": brier_self_consistency,
            "brier_pooled": brier_pooled,
            "sample_size": sample_size,
            "exogenous_sample_size": exogenous_sample_size,
            "self_consistency_fraction": self_consistency_fraction,
            "insufficient_exogenous": insufficient_exogenous,
            "reliability_bins": reliability_bins,
            "per_analyst": per_analyst,
            "rolling_brier": rolling,
            "drift_z": drift_z,
            "drift_alert": drift_alert,
            "drift_threshold": drift_threshold,
            # `resolution_sources` = the resolved_by breakdown;
            # `self_consistency_fraction` quantifies how much of the sample is
            # self-consistency (status_transition) vs exogenous reality.
            "resolution_sources": resolution_sources,
            "self_consistency_only": self_consistency_only,
            # R1-T1.3 (#92) — the acute-binary forecast pilot, in its OWN keys.
            # `brier_forecast_acute` is NEVER pooled into the headline `brier`;
            # it is the only number the project may quote as "forecast" skill,
            # and only once `forecast_acute_ready` AND `brier_skill_score` > 0.
            **forecast_acute,
            "warnings": warnings,
        },
    )


# ---------------------------------------------------------------------------
# Public handler entry point
# ---------------------------------------------------------------------------


async def handle(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: Any | None,
) -> AnalystMethodResult:
    """Sub-handler entry point — see module docstring."""
    bin_count = int(options.get("bin_count", _DEFAULT_BIN_COUNT))
    rolling_weeks = int(options.get("rolling_weeks", _DEFAULT_ROLLING_WEEKS))
    drift_threshold = float(options.get("drift_threshold", _DEFAULT_DRIFT_THRESHOLD))

    rows = list(inputs)
    # The cadence actor hands this handler the GENERIC signals slice
    # (_read_substrate_slice → FROM signals), which has no claimed_confidence /
    # outcome columns and is NOT a calibration input — the old `if not rows`
    # guard saw it as non-empty and SKIPPED the substrate pull, so every cadence
    # run dropped all 50 signal rows as invalid (sample_size=0, brier=null). Only
    # treat `inputs` as pre-shaped calibration rows when they actually carry a
    # `claimed_confidence` (the unit-test / on-demand shape); otherwise pull the
    # resolved hypotheses ourselves. deps-gated so a deps=None test is unchanged.
    calibration_shaped = bool(rows) and all(
        isinstance(r, Mapping) and "claimed_confidence" in r for r in rows
    )
    acute_rows: list[dict[str, Any]] = []
    if not calibration_shaped and deps is not None and bool(
        options.get("pull_from_substrate", True)
    ):
        # Resolve past-horizon predictions FIRST (the exogenous-data generator)
        # so freshly-resolved forecasts enter this same tick's pull (DQ-H2).
        if bool(options.get("resolve_predictions", True)):
            await _resolve_open_predictions(deps, options)
        # R1-T1.3 (#92): issue this week's acute-binary forecasts + resolve any
        # whose forward window has closed+settled, BEFORE the pulls, so freshly
        # graded pilot calls land in this same tick. Both gated + best-effort;
        # neither can break the calibration tick.
        if bool(options.get("issue_acute_forecasts", True)):
            try:
                await forecast_acute.issue_weekly_forecasts(deps, options)
            except Exception as exc:  # noqa: BLE001
                logger.warning("calibration_tracking.acute_issue_failed err=%s", exc)
        if bool(options.get("resolve_acute_forecasts", True)):
            try:
                await forecast_acute.resolve_open_acute_forecasts(deps, options)
            except Exception as exc:  # noqa: BLE001
                logger.warning("calibration_tracking.acute_resolve_failed err=%s", exc)
        rows = await _pull_resolved_claims(deps, options)
        # Pull the pilot rows SEPARATELY — they are reported in their own
        # segregated key and are NEVER added to the headline calibration sample.
        try:
            acute_rows = await forecast_acute.pull_resolved_acute_forecasts(deps, options)
        except Exception as exc:  # noqa: BLE001
            logger.warning("calibration_tracking.acute_pull_failed err=%s", exc)

    kept, warnings = _validate_rows(rows)

    sample_size = len(kept)
    reliability = _reliability_bins(kept, bin_count=bin_count)
    per_analyst = _per_analyst_brier(kept)
    rolling = _rolling_brier(kept, max_weeks=rolling_weeks)
    drift = _drift_z(rolling)
    resolution_sources, self_consistency_only = _resolution_source_breakdown(kept)

    # DQ-H2 honest split: the headline Brier is EXOGENOUS-only; the pooled +
    # self-consistency Briers are diagnostics. A sample dominated by
    # status_transition (self-consistency) can no longer masquerade as
    # "calibrated" — the headline is None (insufficient_exogenous) until enough
    # of the WORLD's verdicts land.
    exo_rows = [r for r in kept if _is_exogenous(r)]
    sc_rows = [r for r in kept if not _is_exogenous(r)]
    brier_exogenous = _brier(exo_rows)
    brier_self_consistency = _brier(sc_rows)
    brier_pooled = _brier(kept)
    self_consistency_fraction = (len(sc_rows) / sample_size) if sample_size else 0.0
    min_exo = int(options.get("min_exogenous", _MIN_EXOGENOUS_FOR_BRIER))
    insufficient_exogenous = len(exo_rows) < min_exo
    headline_brier = None if insufficient_exogenous else brier_exogenous

    # R1-T1.3 (#92) — the pre-registered acute-binary forecast pilot, computed
    # over its OWN resolved rows and reported in its OWN keys (segregated).
    forecast_acute_metrics = _forecast_acute_metrics(
        acute_rows,
        min_sample=int(
            options.get("forecast_acute_min_sample", _FORECAST_ACUTE_MIN_SAMPLE)
        ),
    )

    finding = _build_finding(
        brier=headline_brier,
        sample_size=sample_size,
        reliability_bins=reliability,
        per_analyst=per_analyst,
        rolling=rolling,
        drift_z=drift,
        drift_threshold=drift_threshold,
        resolution_sources=resolution_sources,
        self_consistency_only=self_consistency_only,
        brier_exogenous=brier_exogenous,
        brier_self_consistency=brier_self_consistency,
        brier_pooled=brier_pooled,
        exogenous_sample_size=len(exo_rows),
        self_consistency_fraction=self_consistency_fraction,
        insufficient_exogenous=insufficient_exogenous,
        forecast_acute=forecast_acute_metrics,
        warnings=warnings,
        target_id=options.get("target_id"),
    )
    return AnalystMethodResult(
        finding=finding,
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
    )


__all__ = ["handle"]
