# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""L-174 — Predictor analyst kind.

Topology §5.5 / kind contracts §5. The `predictor` kind takes a window of
recent `signals` rows (event counts, optional per-signal sentiment), fits a
lightweight statistical forecaster, and wraps the numeric forecast in an
LLM-generated narrative that cites the input signals.

Read:    list of substrate `signals` rows (dicts with ``produced_at`` and
         ``data``; the runtime resolves the subscription before calling).
Method:  daily-aggregated AutoARIMA from statsforecast (already a base
         dependency — see pyproject.toml note on `statsforecast>=1.7`).
         Why AutoARIMA over Prophet/ETS:
           * already pinned in pyproject — no new dep.
           * yields conformal prediction intervals without extra wiring.
           * handles short series (15-30 days) more gracefully than Prophet,
             which needs cmdstanpy and ~hundreds of points to be stable.
         Why not Prophet: ~250 MB transitive (cmdstanpy + CmdStan binary),
         and seasonal-trend overkill at the scale of operator-relevant
         windows (days→weeks).
Write:   ``AnalystMethodResult`` whose ``finding: FindingPayload`` carries:
           * ``data["prediction"]`` — full ``PredictionPayload`` dump
             (point estimate, CI bounds, horizon, narrative).
           * ``evidence`` — contributing signal IDs (uuid strings).
           * ``body`` — the LLM narrative (or a deterministic fallback).
         The runtime's dispatcher currently writes via ``OutputKind.FINDING``;
         when L-190 splits ``predictions`` into its own dispatch path, the
         ``data["prediction"]`` blob is the source of truth — no analyst-side
         change required.

LLM boundary: the narrative wrapper accepts any object that satisfies
``LLMHandlerLike`` (from ``legba.runtime.analyst_method``). The narrative is
*optional* — if no handler is supplied, or if the handler raises, the
predictor emits the numeric forecast with a fixed terse narrative ("no
narrative available — stat-model output only"). Tests can pass a stub.

No DSPy module is exposed yet (predictor is primarily statistical); the
narrative LLM call will be wrapped as a DSPy Predict module in Phase 6 once
L-105 lands the optimizer's signature surface.
"""

from __future__ import annotations

import json
import logging
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping

import numpy as np

from ..nats import SIGNALS_EXCLUDE_BACKFILL_SQL
from ..provenance.models import FindingPayload, PredictionPayload
from ...runtime.analyst_method import AnalystMethodResult, LLMHandlerLike

logger = logging.getLogger(__name__)


def _statsforecast_available() -> bool:
    """Startup smoke-check: can AutoARIMA actually be imported?

    DQ-H1: a runtime-image slimming step deleted ``numpy/testing`` (a PUBLIC
    numpy module statsforecast imports at import time), so
    ``from statsforecast.models import AutoARIMA`` raised ImportError and EVERY
    forecast silently downgraded to naive_mean — a non-forecaster behind green
    status. We probe ONCE at module import and log LOUD if it's broken, so the
    degradation is visible in the boot log instead of hiding behind per-run
    WARNINGs nobody reads.
    """
    try:
        from statsforecast.models import AutoARIMA  # noqa: F401
        return True
    except Exception:  # noqa: BLE001 — any import failure means no real forecaster
        return False


_STATSFORECAST_OK = _statsforecast_available()
if _STATSFORECAST_OK:
    logger.info("predictor.statsforecast.ok AutoARIMA import succeeded")
else:
    logger.error(
        "predictor.statsforecast.UNAVAILABLE — AutoARIMA import FAILED; ALL "
        "forecasts will silently fall back to naive_mean. Most likely cause: "
        "numpy.testing missing from the image (a slimming step deleted it). "
        "Fix the image, do not ship a non-forecaster. (DQ-H1)"
    )


KIND_NAME = "predictor"
"""Registered analyst-kind name (matches ``AnalystKind.PREDICTOR``)."""


# Host-discovered constants for the per-kind dispatch path.
# The predictor writes a PredictionPayload row (not a FindingPayload).  The
# host's analyst-output dispatcher reads OUTPUT_KIND to pick the table +
# pydantic model.  See ``legba.runtime.dapr_actors`` for the wire-up.
from ..provenance.kinds import OutputKind as _OutputKind  # noqa: E402

OUTPUT_KIND: _OutputKind = _OutputKind.PREDICTION
# READ_SLICE is a custom daily-aggregating reader defined below (DQ-H1(b)):
# the default signals slice caps at 50 raw rows by recency, collapsing a
# high-volume target's daily series to 1-2 buckets. See the READ_SLICE def.

# Wave B prereq #4 — DSPy module path for the OPTIONAL narrative wrapper.
# The predictor's stat core is deterministic Python; the LLM call only
# happens to write the "why this number" paragraph and is itself optional
# (PredictorDeps.llm = None bypasses this module entirely).
PROMPT_MODULE_PATH: str = "legba.prompts.predictor.v1"


def build_prompt_module() -> Any:
    """Construct and return the DSPy module for the predictor's narrative.

    Wave B prereq #4: returns a real
    :class:`legba.prompts.predictor.v1.PredictorNarrative`.  The DSPy
    module wraps the OPTIONAL narrative LLM call only — the stat core
    (AutoARIMA + naive-mean fallback) is pure Python and lives in
    :func:`_fit_forecast` further down this module.

    Lazy-imports so this file imports cleanly when dspy isn't installed;
    raises :class:`ModuleNotFoundError` otherwise (matching the
    inline_target contract).
    """
    from legba.prompts.predictor.v1 import build as _build
    return _build()


# ---------------------------------------------------------------------------
# Tuning knobs — kept module-level so tests can override without subclassing.
# ---------------------------------------------------------------------------


DEFAULT_HORIZON_DAYS = 7
"""Forecast horizon — one operator-week ahead. Matches L-174 spec."""

DEFAULT_CI_LEVEL = 80
"""Default confidence-interval percentage emitted with the forecast."""

MIN_OBSERVATIONS = 5
"""Below this many daily buckets we skip the stat model and fall back to a
naive mean forecast — AutoARIMA needs at least a handful of points to be
meaningful even with conformal CIs."""

MAX_INPUT_SIGNALS_FOR_NARRATIVE = 15
"""Cap on substrate rows passed into the LLM narrative prompt — keeps the
prompt short and the cost predictable."""


# ---------------------------------------------------------------------------
# Internal: timeseries aggregation
# ---------------------------------------------------------------------------


@dataclass
class _DailySeries:
    """Daily aggregation of the signal window.

    ``counts`` is len-N (one slot per day in the observed range).
    ``sentiments`` is the per-day mean sentiment when at least one signal in
    that day exposed a numeric sentiment under ``data.sentiment``; missing
    days are NaN.
    """

    start: date
    counts: np.ndarray  # shape (N,), int counts
    sentiments: np.ndarray  # shape (N,), float (NaN where missing)
    signal_ids: list[str]  # contributing signal IDs in input order
    signal_id_by_day: dict[date, list[str]]


def _parse_dt(value: Any) -> datetime | None:
    """Coerce common ``produced_at`` shapes (datetime, ISO str) into UTC."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _aggregate_daily(inputs: list[Mapping[str, Any]]) -> _DailySeries | None:
    """Bin signals into a contiguous daily series.

    Empty input or all-undated input → ``None`` (caller falls back to a
    no-forecast path).
    """
    by_day: dict[date, list[Mapping[str, Any]]] = defaultdict(list)
    for row in inputs:
        dt = _parse_dt(row.get("produced_at"))
        if dt is None:
            continue
        by_day[dt.date()].append(row)
    if not by_day:
        return None

    start = min(by_day.keys())
    end = max(by_day.keys())
    n_days = (end - start).days + 1
    counts = np.zeros(n_days, dtype=np.float64)
    sentiments = np.full(n_days, np.nan, dtype=np.float64)
    signal_ids: list[str] = []
    signal_id_by_day: dict[date, list[str]] = {}

    for i in range(n_days):
        day = start + timedelta(days=i)
        rows = by_day.get(day, [])
        # The predictor's READ_SLICE delivers ONE pre-aggregated bucket per day
        # carrying an explicit ``count`` + a few ``sample_ids`` (DQ-H1(b): SQL
        # daily aggregation over the full window, so a high-volume series isn't
        # collapsed by the 50-row recency cap). Raw signal rows (the default
        # reader / tests) carry neither — each counts as 1. Honour both.
        day_count = 0.0
        sent_vals: list[float] = []
        per_day_ids: list[str] = []
        for r in rows:
            raw_count = r.get("count")
            if isinstance(raw_count, (int, float)) and not isinstance(raw_count, bool):
                day_count += float(raw_count)
            else:
                day_count += 1.0
            data = r.get("data") or {}
            if isinstance(data, dict):
                raw_sent = data.get("sentiment")
                if isinstance(raw_sent, (int, float)) and not math.isnan(float(raw_sent)):
                    sent_vals.append(float(raw_sent))
            sample = r.get("sample_ids")
            if isinstance(sample, list) and sample:
                for sid in sample:
                    sid_str = str(sid)
                    per_day_ids.append(sid_str)
                    signal_ids.append(sid_str)
            else:
                rid = r.get("id")
                if rid is not None:
                    rid_str = str(rid)
                    per_day_ids.append(rid_str)
                    signal_ids.append(rid_str)
        counts[i] = day_count
        if sent_vals:
            sentiments[i] = float(sum(sent_vals) / len(sent_vals))
        if per_day_ids:
            signal_id_by_day[day] = per_day_ids

    return _DailySeries(
        start=start,
        counts=counts,
        sentiments=sentiments,
        signal_ids=signal_ids,
        signal_id_by_day=signal_id_by_day,
    )


# ---------------------------------------------------------------------------
# Custom READ_SLICE — daily-aggregated counts over the FULL forecast window
# ---------------------------------------------------------------------------


# Forecast read window default — generous so the daily series has enough
# buckets for AutoARIMA even when a descriptor doesn't pin a time_window.
DEFAULT_FORECAST_WINDOW_HOURS = 720  # 30 days


def _resolve_window_hours(descriptor: Any) -> int:
    """Read the forecast window (hours) off the descriptor, mirroring the
    runtime's default slice resolution (subscription.targets.time_window), with
    a forecasting-friendly 30-day floor when unset."""
    sub = getattr(descriptor, "subscription", None)
    if sub is None:
        return DEFAULT_FORECAST_WINDOW_HOURS
    targets = getattr(sub, "targets", None)
    cand = (
        (getattr(targets, "time_window", None) if targets is not None else None)
        or getattr(sub, "time_window", None)
        or getattr(sub, "time_window_hours", None)
    )
    if cand is None:
        return DEFAULT_FORECAST_WINDOW_HOURS
    if isinstance(cand, bool):
        return DEFAULT_FORECAST_WINDOW_HOURS
    if isinstance(cand, (int, float)):
        return max(24, int(cand))
    if isinstance(cand, str):
        try:
            return max(24, int(cand[:-1] if cand.endswith("h") else cand))
        except ValueError:
            return DEFAULT_FORECAST_WINDOW_HOURS
    return DEFAULT_FORECAST_WINDOW_HOURS


async def READ_SLICE(  # noqa: N802 — host-discovered constant alias
    conn,  # type: ignore[no-untyped-def]
    *,
    descriptor,  # type: ignore[no-untyped-def]
    target_filter,  # type: ignore[no-untyped-def]
) -> list[dict[str, Any]]:
    """Daily-aggregated signal counts over the FULL forecast window.

    DQ-H1(b): the default signals slice (``_read_substrate_slice``) caps at 50
    raw rows by recency. For a high-volume target that collapses the daily
    series to 1-2 buckets, so the forecast mechanically returns ~50/n instead
    of a real trend. This reader aggregates counts in SQL over the whole window
    — one bucket per day — using the SAME source_id / geo narrowing the runtime
    slice uses, so AutoARIMA sees the true series. Returns daily-bucket rows
    (``produced_at`` + ``count`` + ``sample_ids``) that :func:`_aggregate_daily`
    honours. Best-effort: any failure returns ``[]`` (caller emits a noop).
    """
    window_hours = _resolve_window_hours(descriptor)
    source_ids: list[str] = []
    target_geo: list[str] = []
    if target_filter:
        try:
            trow = await conn.fetchrow(
                "SELECT body FROM target_descriptors "
                "WHERE descriptor_id = $1 AND is_head = TRUE",
                target_filter,
            )
            if trow and trow["body"]:
                tbody = trow["body"]
                if isinstance(tbody, str):
                    tbody = json.loads(tbody)
                for sref in (tbody.get("sources") or []):
                    sid = sref.get("source_id") if isinstance(sref, dict) else None
                    if sid:
                        source_ids.append(sid)
                scope = (tbody.get("scope") or {}) if isinstance(tbody, dict) else {}
                target_geo = [g for g in (scope.get("geo") or []) if g]
        except Exception:  # noqa: BLE001 — degrade to tenant-wide pool
            source_ids, target_geo = [], []

    # Fresh-window daily series — exclude backfill (S4-T4). Backfilled signals
    # all carry fetched_at=load-time, so they would pile into a single day's
    # bucket and distort the recency series the forecaster reads; a backfill is
    # historical accumulation, not fresh daily volume.
    clauses = [
        f"fetched_at > NOW() - INTERVAL '{int(window_hours)} hours'",
        SIGNALS_EXCLUDE_BACKFILL_SQL,
    ]
    params: list[Any] = []
    if source_ids:
        params.append(source_ids)
        clauses.append(f"source_id = ANY(${len(params)})")
    if target_geo:
        params.append(target_geo)
        clauses.append(f"geo && ${len(params)}::text[]")
    where = "WHERE " + " AND ".join(clauses)
    try:
        rows = await conn.fetch(
            f"""
            SELECT date_trunc('day', fetched_at) AS day,
                   count(*)::int AS cnt,
                   (array_agg(id::text ORDER BY fetched_at DESC))[1:5] AS sample_ids
            FROM signals
            {where}
            GROUP BY 1
            ORDER BY 1
            """,
            *params,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("predictor.read_slice.query_failed err=%s", exc)
        return []

    out: list[dict[str, Any]] = []
    for r in rows:
        day = r["day"]
        sample_ids = [str(s) for s in (r["sample_ids"] or [])]
        cnt = int(r["cnt"])
        out.append({
            "produced_at": day.isoformat() if hasattr(day, "isoformat") else str(day),
            "count": cnt,
            "sample_ids": sample_ids,
            "id": sample_ids[0] if sample_ids else None,
            "title": f"{cnt} signals on {day.date().isoformat() if hasattr(day, 'date') else day}",
        })
    return out


# ---------------------------------------------------------------------------
# Statistical forecast
# ---------------------------------------------------------------------------


@dataclass
class _ForecastResult:
    """Stat-model output — point estimate + symmetric CI bounds."""

    point: float        # mean predicted count at the final horizon step
    lo: float           # CI lower bound at the final horizon step
    hi: float           # CI upper bound at the final horizon step
    horizon_days: int
    ci_level: int
    method: str         # "auto_arima" or "naive_mean"
    horizon_series: list[float]  # full per-step point forecast (len == horizon_days)


def _forecast_naive(series: np.ndarray, horizon_days: int, ci_level: int) -> _ForecastResult:
    """Fallback when there's not enough history for a real stat model.

    Point = mean of observed counts. CI = mean ± z·std (with a floor on
    width so degenerate series don't emit a zero-width interval).
    """
    mean = float(np.mean(series)) if series.size else 0.0
    std = float(np.std(series)) if series.size > 1 else 0.0
    # ~ z for 80% CI; we tabulate a handful so we don't drag scipy in.
    z_table = {80: 1.282, 90: 1.645, 95: 1.96, 99: 2.576}
    z = z_table.get(ci_level, 1.282)
    half_width = max(z * std, 0.5)
    return _ForecastResult(
        point=mean,
        lo=max(0.0, mean - half_width),
        hi=mean + half_width,
        horizon_days=horizon_days,
        ci_level=ci_level,
        method="naive_mean",
        horizon_series=[mean] * horizon_days,
    )


def _forecast_arima(
    series: np.ndarray, horizon_days: int, ci_level: int,
) -> _ForecastResult:
    """Fit AutoARIMA and predict ``horizon_days`` ahead with a CI band.

    Defensive: any statsforecast/numerical error falls back to ``_forecast_naive``.
    """
    # Lazy import so test envs without numpy-stack pain still import this module
    # (statsforecast is a hard dependency, but lazy-imports keep startup light).
    try:
        from statsforecast.models import AutoARIMA
    except ImportError as exc:  # pragma: no cover - dep is pinned
        # LOUD: a pinned base dep failing to import means we are silently
        # shipping a non-forecaster (every forecast → naive_mean). DQ-H1.
        logger.error(
            "predictor.forecast.statsforecast_missing err=%s — forecasts are "
            "downgrading to naive_mean; this is a broken image, not a no-op",
            exc,
        )
        return _forecast_naive(series, horizon_days, ci_level)

    try:
        # Weekly seasonality is the natural human-scale period for "events
        # per day"; AutoARIMA degrades gracefully if it isn't present.
        model = AutoARIMA(season_length=7, stationary=False)
        fc = model.forecast(
            y=series.astype(np.float64),
            h=horizon_days,
            level=[ci_level],
        )
    except Exception as exc:
        logger.warning("predictor.forecast.arima_failed err=%s", exc)
        return _forecast_naive(series, horizon_days, ci_level)

    mean_arr = np.asarray(fc.get("mean"))
    lo_key = f"lo-{ci_level}"
    hi_key = f"hi-{ci_level}"
    lo_arr = np.asarray(fc.get(lo_key)) if lo_key in fc else None
    hi_arr = np.asarray(fc.get(hi_key)) if hi_key in fc else None
    if mean_arr is None or mean_arr.size == 0:
        return _forecast_naive(series, horizon_days, ci_level)

    point = float(mean_arr[-1])
    if lo_arr is not None and lo_arr.size:
        lo = float(lo_arr[-1])
    else:
        lo = point
    if hi_arr is not None and hi_arr.size:
        hi = float(hi_arr[-1])
    else:
        hi = point

    # Clip to non-negative — these are event counts.
    point = max(0.0, point)
    lo = max(0.0, lo)
    hi = max(lo, hi)

    return _ForecastResult(
        point=point,
        lo=lo,
        hi=hi,
        horizon_days=horizon_days,
        ci_level=ci_level,
        method="auto_arima",
        horizon_series=[max(0.0, float(v)) for v in mean_arr.tolist()],
    )


# ---------------------------------------------------------------------------
# Narrative wrapper
# ---------------------------------------------------------------------------


_NARRATIVE_FALLBACK = "no narrative available — stat-model output only"


def _render_narrative_prompt(
    *,
    inputs: list[Mapping[str, Any]],
    series: _DailySeries,
    forecast: _ForecastResult,
    target_id: str | None,
) -> str:
    """Build the LLM prompt asking for a "why this number" narrative."""
    recent_summaries: list[str] = []
    for i, row in enumerate(inputs[:MAX_INPUT_SIGNALS_FOR_NARRATIVE], start=1):
        title = str(row.get("title") or "(untitled)")[:200]
        produced_at = row.get("produced_at")
        rid = row.get("id")
        recent_summaries.append(
            f"[{i}] id={rid} produced_at={produced_at} title={title}"
        )

    obs_window = f"{series.start.isoformat()} → " + (
        series.start + timedelta(days=len(series.counts) - 1)
    ).isoformat()

    return (
        f"You are an analyst writing the explanatory paragraph for a "
        f"forecast.\n"
        f"Target: {target_id or 'unspecified'}\n"
        f"Observed window: {obs_window} "
        f"(daily event counts: {series.counts.tolist()})\n"
        f"Forecast method: {forecast.method}\n"
        f"Forecast horizon: {forecast.horizon_days} days\n"
        f"Point estimate (events/day, final step): {forecast.point:.2f}\n"
        f"{forecast.ci_level}% CI: [{forecast.lo:.2f}, {forecast.hi:.2f}]\n\n"
        f"Recent signals (cite the most relevant by id):\n"
        + "\n".join(recent_summaries)
        + "\n\nWrite 3-5 sentences explaining the forecast: what direction "
        "the recent signal flow points, which signal IDs are most "
        "load-bearing, and what would falsify the prediction. Reference "
        "signal IDs inline as 'signal:<id>'. Do not invent IDs."
    )


async def _generate_narrative(
    *,
    llm: LLMHandlerLike | None,
    prompt: str,
) -> tuple[str, dict[str, int]]:
    """Call the LLM for the narrative. Returns ``(text, usage_dict)``.

    Errors → fallback narrative + empty usage dict.
    """
    if llm is None:
        return _NARRATIVE_FALLBACK, {}
    try:
        response = await llm.chat_complete(
            [{"role": "user", "content": prompt}],
            max_tokens=400,
            temperature=0.2,
            system=(
                "You are a careful intelligence analyst. Cite contributing "
                "signal IDs verbatim and never invent ones not provided."
            ),
        )
    except Exception as exc:
        logger.warning("predictor.narrative.llm_failed err=%s", exc)
        return _NARRATIVE_FALLBACK, {}
    content = (getattr(response, "content", None) or "").strip()
    if not content:
        return _NARRATIVE_FALLBACK, {}
    usage_raw = getattr(response, "usage", None)
    usage_dict = {
        "prompt_tokens": getattr(usage_raw, "prompt_tokens", 0) if usage_raw else 0,
        "completion_tokens": (
            getattr(usage_raw, "completion_tokens", 0) if usage_raw else 0
        ),
        "reasoning_tokens": (
            getattr(usage_raw, "reasoning_tokens", 0) if usage_raw else 0
        ),
    }
    return content, usage_dict


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


@dataclass
class PredictorDeps:
    """Sub-connection ports for the predictor kind.

    Kept loose for now — the runtime wires this via the analyst descriptor's
    ``Property.StackRef`` fields once the optimizer / discovery loop bolts on
    (Phase 6 / L-176). The LLM is the only port the run path consults.
    """

    llm: LLMHandlerLike | None = None
    horizon_days: int = DEFAULT_HORIZON_DAYS
    ci_level: int = DEFAULT_CI_LEVEL


async def run_method(
    inputs: list[Mapping[str, Any]],
    options: Mapping[str, Any],
    deps: PredictorDeps | None = None,
) -> AnalystMethodResult:
    """Run the predictor.

    The runtime calls this with the substrate slice already resolved
    (per ``_read_substrate_slice`` in dapr_actors). ``options`` carries
    ``analyst_id``, ``analyst_version``, ``run_id``, and optionally
    ``target_id``. ``deps`` is the predictor's port bundle; passing
    ``None`` runs in stat-only mode with no narrative.
    """
    deps = deps or PredictorDeps()
    horizon_days = max(1, int(deps.horizon_days))
    ci_level = int(deps.ci_level)
    target_id = options.get("target_id")
    analyst_id = options.get("analyst_id") or "predictor"

    aggregated = _aggregate_daily(list(inputs))
    if aggregated is None or aggregated.counts.size == 0:
        # No usable history — emit a no-forecast finding instead of failing.
        empty_pred = PredictionPayload(
            hypothesis=(
                f"No forecast emitted for {target_id or 'unspecified target'}: "
                "no dated signals in the read window."
            ),
            source_type="stat_forecast",
            category="event_count_forecast",
            region=str(target_id) if target_id else "",
            status="open",
            confidence=0.0,
            evidence_for=[],
            evidence_against=[],
        )
        finding = FindingPayload(
            title=f"Predictor noop — {target_id or 'unspecified'}",
            body="No dated signals in the read window; predictor produced no forecast.",
            confidence=0.0,
            evidence=[],
            tags=["predictor", "noop"],
            data={
                "prediction": empty_pred.model_dump(),
                "forecast_method": "none",
                "horizon_days": horizon_days,
            },
        )
        return AnalystMethodResult(finding=finding, usage={})

    series = aggregated.counts
    if series.size < MIN_OBSERVATIONS:
        forecast = _forecast_naive(series, horizon_days, ci_level)
    else:
        forecast = _forecast_arima(series, horizon_days, ci_level)

    prompt = _render_narrative_prompt(
        inputs=list(inputs),
        series=aggregated,
        forecast=forecast,
        target_id=str(target_id) if target_id else None,
    )
    narrative, usage = await _generate_narrative(llm=deps.llm, prompt=prompt)

    horizon_end = aggregated.start + timedelta(
        days=len(aggregated.counts) - 1 + forecast.horizon_days
    )
    hypothesis_text = (
        f"Forecast: {forecast.point:.2f} events/day at +{forecast.horizon_days}d "
        f"(by {horizon_end.isoformat()}); {forecast.ci_level}% CI "
        f"[{forecast.lo:.2f}, {forecast.hi:.2f}]; method={forecast.method}."
    )

    # Confidence shrinks with CI width relative to point estimate. Bounded [0,1].
    if forecast.hi > forecast.lo and forecast.point > 0:
        rel_width = (forecast.hi - forecast.lo) / max(forecast.point, 1e-6)
        # Map rel_width=0 -> 0.9, rel_width=2 -> ~0.3, capped.
        confidence = max(0.1, min(0.9, 0.9 / (1.0 + 0.6 * rel_width)))
    else:
        confidence = 0.4

    prediction = PredictionPayload(
        hypothesis=hypothesis_text,
        source_type="stat_forecast",
        category="event_count_forecast",
        region=str(target_id) if target_id else "",
        status="open",
        confidence=float(confidence),
        evidence_for=aggregated.signal_ids[:50],
        evidence_against=[],
    )
    # PredictionPayload allows extras (model_config = extra="allow"), so we
    # stash the numerics on the model itself for callers that introspect.
    prediction_extras = {
        "point_estimate": forecast.point,
        "ci_lower": forecast.lo,
        "ci_upper": forecast.hi,
        "ci_level": forecast.ci_level,
        "horizon_days": forecast.horizon_days,
        "horizon_end": horizon_end.isoformat(),
        "narrative": narrative,
        "method": forecast.method,
        "contributing_signal_ids": aggregated.signal_ids[:200],
        "horizon_series": forecast.horizon_series,
    }
    prediction_dump = prediction.model_dump()
    prediction_dump.update(prediction_extras)

    finding_body = narrative if narrative != _NARRATIVE_FALLBACK else (
        f"{hypothesis_text}\n\n{_NARRATIVE_FALLBACK}."
    )
    finding = FindingPayload(
        title=(
            f"Predictor forecast +{forecast.horizon_days}d for "
            f"{target_id or 'unspecified'}: {forecast.point:.2f} events/day"
        )[:2048],
        body=finding_body[:65536],
        confidence=float(confidence),
        evidence=aggregated.signal_ids[:50],
        tags=["predictor", forecast.method, f"horizon_{forecast.horizon_days}d"],
        data={
            "prediction": prediction_dump,
            "forecast_method": forecast.method,
            "horizon_days": forecast.horizon_days,
            "ci_level": forecast.ci_level,
            "observed_days": int(len(aggregated.counts)),
            "observed_total_events": int(aggregated.counts.sum()),
            "analyst_id": analyst_id,
        },
    )
    return AnalystMethodResult(finding=finding, usage=usage)


__all__ = [
    "KIND_NAME",
    "OUTPUT_KIND",
    "PROMPT_MODULE_PATH",
    "READ_SLICE",
    "PredictorDeps",
    "DEFAULT_HORIZON_DAYS",
    "DEFAULT_CI_LEVEL",
    "MIN_OBSERVATIONS",
    "build_prompt_module",
    "run_method",
]
