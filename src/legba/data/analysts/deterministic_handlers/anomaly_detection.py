# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``anomaly_detection`` sub-handler — L-006 sub-split B.

Three deterministic detectors over the most recent signal window:

  1. **Rate spikes** — z-score of the latest bucket's signal-count
     relative to the trailing N-bucket distribution. Threshold-controlled.
  2. **Sentiment shifts** — z-score of the latest bucket's mean sentiment
     against the trailing distribution. Two-sided; reports both polarity
     and magnitude.
  3. **Novel entity emergence** — entities appearing in the latest
     window with zero observed in the trailing window. Counted + ranked
     by current-window frequency.

All three run on already-bucketed numeric arrays — no LLM, no graph
traversal. The handler accepts pre-bucketed series in ``inputs`` so the
unit tests can drive synthetic spikes without a live substrate; the
``deps`` path buckets straight off the PRIMARY Postgres pool
(``deps.pg_pool``) via ``time_bucket()`` and is used by the runtime.

Input row shape (canonical):
    {
        "bucket_ts": iso8601 str,
        "category": str | None,
        "count": int,                       # signals in this bucket
        "sentiment_mean": float | None,     # -1..+1
        "entities": [str, ...],             # entities seen this bucket
    }

Buckets are assumed already aligned to the same cadence (caller's
responsibility — the analyst descriptor should set the ``time_bucket``
interval to a stable value e.g. 1 hour).

Output ``data`` keys:
    rate_spikes     [{bucket_ts, z, count, mean, std, category}]
    sentiment_shifts [{bucket_ts, z, mean, baseline_mean, baseline_std, category}]
    novel_entities  [{entity, current_count}]
    bucket_count    int — total buckets considered
"""

from __future__ import annotations

import logging
import math
from collections import Counter, defaultdict
from typing import Any, Mapping, Sequence

import numpy as np

from ...provenance.models import FindingPayload
from ....runtime.analyst_method import AnalystMethodResult

logger = logging.getLogger(__name__)

# Defaults — overridable via options.
_DEFAULT_Z_THRESHOLD = 2.5
_DEFAULT_WINDOW_BUCKETS = 24
_DEFAULT_NOVEL_LOOKBACK = 24  # buckets of "trailing" history to define novelty


# ---------------------------------------------------------------------------
# Detectors
# ---------------------------------------------------------------------------


def _z_score(value: float, baseline: Sequence[float]) -> tuple[float, float, float]:
    """Z-score of ``value`` against the empirical mean+std of baseline.

    Returns ``(z, mean, std)``. Handles two degenerate cases explicitly:

      * Empty baseline → ``(0.0, 0.0, 0.0)`` (no signal).
      * Zero-variance baseline + value != mean → use the magnitude of
        deviation relative to ``max(mean, 1.0)`` as a *pseudo-z*. This
        keeps the detector responsive when a flat baseline gets a clear
        outlier (the alternative — never flagging — silently misses
        every sudden spike off a quiet history, which is the worst kind
        of false negative for anomaly detection).
      * Zero-variance baseline + value == mean → ``(0.0, mean, 0.0)``.
    """
    if not baseline:
        return 0.0, 0.0, 0.0
    arr = np.asarray(baseline, dtype=float)
    mean = float(arr.mean())
    # ddof=1 for sample stdev; fall back to 0 on tiny samples.
    std = float(arr.std(ddof=1)) if arr.size > 1 else 0.0
    if std == 0.0 or math.isnan(std):
        if value == mean:
            return 0.0, mean, 0.0
        # Pseudo-z: deviation in mean-relative units, with a floor of 1
        # so a tiny mean (e.g. 0.0) doesn't blow up to infinity.
        denom = max(abs(mean), 1.0)
        return float((value - mean) / denom), mean, 0.0
    return float((value - mean) / std), mean, std


def _detect_rate_spikes(
    series_by_category: Mapping[str, list[dict[str, Any]]],
    *,
    window: int,
    threshold: float,
) -> list[dict[str, Any]]:
    """Per-category, flag buckets where ``count`` z-score > threshold."""
    spikes: list[dict[str, Any]] = []
    for category, series in series_by_category.items():
        if len(series) < 3:  # need at least 3 buckets to z-score
            continue
        # Walk forward; baseline = trailing `window` buckets.
        for i in range(1, len(series)):
            current = series[i]
            baseline = [
                float(b.get("count", 0))
                for b in series[max(0, i - window): i]
            ]
            count = float(current.get("count", 0))
            z, mean, std = _z_score(count, baseline)
            if z > threshold:
                spikes.append({
                    "bucket_ts": current.get("bucket_ts"),
                    "category": category,
                    "z": z,
                    "count": count,
                    "mean": mean,
                    "std": std,
                })
    # Stable order: most extreme first.
    spikes.sort(key=lambda s: -s["z"])
    return spikes


def _detect_sentiment_shifts(
    series_by_category: Mapping[str, list[dict[str, Any]]],
    *,
    window: int,
    threshold: float,
) -> list[dict[str, Any]]:
    """Per-category, flag buckets where sentiment_mean shifts > threshold (abs z)."""
    shifts: list[dict[str, Any]] = []
    for category, series in series_by_category.items():
        if len(series) < 3:
            continue
        for i in range(1, len(series)):
            current = series[i]
            cur_sent = current.get("sentiment_mean")
            if cur_sent is None:
                continue
            baseline = [
                float(b["sentiment_mean"])
                for b in series[max(0, i - window): i]
                if b.get("sentiment_mean") is not None
            ]
            if len(baseline) < 2:
                continue
            z, mean, std = _z_score(float(cur_sent), baseline)
            if abs(z) > threshold:
                shifts.append({
                    "bucket_ts": current.get("bucket_ts"),
                    "category": category,
                    "z": z,
                    "mean": float(cur_sent),
                    "baseline_mean": mean,
                    "baseline_std": std,
                })
    shifts.sort(key=lambda s: -abs(s["z"]))
    return shifts


def _detect_novel_entities(
    rows: list[dict[str, Any]],
    *,
    lookback: int,
) -> list[dict[str, Any]]:
    """Entities seen in the latest bucket but absent from the trailing window.

    Trailing window is the ``lookback`` buckets immediately before the
    latest bucket. Returns up to 100 newcomers ranked by current count.
    """
    if not rows:
        return []
    # Order rows by bucket_ts so "latest" is well-defined.
    ordered = sorted(rows, key=lambda r: r.get("bucket_ts") or "")
    latest = ordered[-1]
    latest_entities: list[str] = list(latest.get("entities") or [])
    if not latest_entities:
        return []
    # Trailing baseline.
    history_rows = ordered[max(0, len(ordered) - 1 - lookback): -1]
    seen_history: set[str] = set()
    for r in history_rows:
        for e in (r.get("entities") or []):
            seen_history.add(str(e))
    latest_counter = Counter(str(e) for e in latest_entities)
    novelties = [
        {"entity": e, "current_count": c}
        for e, c in latest_counter.most_common(100)
        if e not in seen_history
    ]
    return novelties


# ---------------------------------------------------------------------------
# Input shaping
# ---------------------------------------------------------------------------


def _group_by_category(
    rows: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Group time-bucketed rows by category for per-category detectors."""
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        cat = str(row.get("category") or "_all")
        grouped[cat].append(row)
    # Order each series by timestamp.
    for cat in grouped:
        grouped[cat].sort(key=lambda r: r.get("bucket_ts") or "")
    return dict(grouped)


# ---------------------------------------------------------------------------
# Optional live-substrate pull (PRIMARY Postgres)
# ---------------------------------------------------------------------------


async def _pull_bucketed_signals(
    deps: Any,
    options: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Best-effort: pull recent bucketed signal counts off the primary pool.

    Buckets directly on the PRIMARY Postgres pool (``deps.pg_pool``) using
    the ``time_bucket()`` function. Returns ``[]`` if deps doesn't carry a
    usable pool or the query fails. The shape produced matches the
    canonical input row form.
    """
    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    if pool is None:
        return []
    bucket_interval = options.get("bucket_interval", "1 hour")
    lookback_hours = int(options.get("lookback_hours", 48))
    sql = (
        "SELECT "
        f"  time_bucket('{bucket_interval}', produced_at) AS bucket_ts, "
        "  category, "
        "  COUNT(*) AS count "
        "FROM signals "
        f"WHERE produced_at > NOW() - INTERVAL '{lookback_hours} hours' "
        "GROUP BY bucket_ts, category "
        "ORDER BY bucket_ts ASC"
    )
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql)
    except Exception as exc:
        logger.warning("anomaly_detection.bucket_pull_failed err=%s", exc)
        return []
    return [
        {
            "bucket_ts": (
                row["bucket_ts"].isoformat()
                if hasattr(row["bucket_ts"], "isoformat")
                else str(row["bucket_ts"])
            ),
            "category": row["category"],
            "count": int(row["count"]),
            "sentiment_mean": None,
            "entities": [],
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Finding assembly
# ---------------------------------------------------------------------------


def _build_finding(
    *,
    rate_spikes: list[dict[str, Any]],
    sentiment_shifts: list[dict[str, Any]],
    novel_entities: list[dict[str, Any]],
    bucket_count: int,
    z_threshold: float,
    warnings: list[str],
    target_id: str | None,
) -> FindingPayload:
    sev_tags: list[str] = ["deterministic", "anomaly_detection"]
    if rate_spikes:
        sev_tags.append("rate_spike_present")
    if sentiment_shifts:
        sev_tags.append("sentiment_shift_present")
    if novel_entities:
        sev_tags.append("novel_entity_present")
    title = (
        f"Anomaly scan: spikes={len(rate_spikes)} "
        f"sentiment_shifts={len(sentiment_shifts)} "
        f"novel_entities={len(novel_entities)}"
        f"{' for ' + target_id if target_id else ''}"
    )
    body = (
        f"buckets_scanned={bucket_count} z_threshold={z_threshold}\n"
        f"top_spike_z={rate_spikes[0]['z']:.2f}\n" if rate_spikes else
        f"buckets_scanned={bucket_count} z_threshold={z_threshold}\n"
    )
    return FindingPayload(
        title=title[:2048],
        body=body[:65536],
        confidence=1.0,
        evidence=[],
        tags=sev_tags,
        data={
            "sub_handler": "anomaly_detection",
            "rate_spikes": rate_spikes,
            "sentiment_shifts": sentiment_shifts,
            "novel_entities": novel_entities,
            "bucket_count": bucket_count,
            "z_threshold": z_threshold,
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
    warnings: list[str] = []
    z_threshold = float(options.get("z_threshold", _DEFAULT_Z_THRESHOLD))
    window = int(options.get("window_buckets", _DEFAULT_WINDOW_BUCKETS))
    novel_lookback = int(options.get("novel_lookback", _DEFAULT_NOVEL_LOOKBACK))

    rows = list(inputs)
    # ``pull_from_timescale`` kept as a back-compat alias for the option key;
    # the pull itself buckets off the PRIMARY Postgres pool, not Timescale.
    pull_enabled = bool(
        options.get("pull_from_substrate", options.get("pull_from_timescale", True))
    )
    if not rows and deps is not None and pull_enabled:
        rows = await _pull_bucketed_signals(deps, options)
        if not rows:
            warnings.append("anomaly_detection.no_data_from_substrate")

    grouped = _group_by_category(rows)
    rate_spikes = _detect_rate_spikes(
        grouped, window=window, threshold=z_threshold
    )
    sentiment_shifts = _detect_sentiment_shifts(
        grouped, window=window, threshold=z_threshold
    )
    novel_entities = _detect_novel_entities(rows, lookback=novel_lookback)

    finding = _build_finding(
        rate_spikes=rate_spikes,
        sentiment_shifts=sentiment_shifts,
        novel_entities=novel_entities,
        bucket_count=len(rows),
        z_threshold=z_threshold,
        warnings=warnings,
        target_id=options.get("target_id"),
    )
    return AnalystMethodResult(
        finding=finding,
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
    )


__all__ = ["handle"]
