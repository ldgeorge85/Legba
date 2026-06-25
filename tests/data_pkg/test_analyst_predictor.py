# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the L-174 ``predictor`` analyst kind.

The predictor is a pure-function async handler — no substrate I/O at the
``run_method`` boundary — so these tests run without containers. Only the
LLM port is stubbed; the statsforecast call is exercised for real.

Coverage:

  * synthetic 30-day timeseries → forecast is finite, non-negative,
    horizon-length matches the configured value, and the final-step point
    estimate lands "in the neighborhood" of recent observations
    (continuity check, not a precision claim).
  * narrative is generated when the LLM port is supplied, and it actually
    cites contributing signal IDs that the predictor passed through.
  * LLM raising → falls back to "no narrative" without dropping the
    numeric forecast.
  * empty input → predictor emits a structured noop instead of crashing.
  * confidence interval is well-formed (lo <= point <= hi).
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from uuid import uuid4

from legba.data.analysts.predictor import (
    DEFAULT_CI_LEVEL,
    DEFAULT_FORECAST_WINDOW_HOURS,
    DEFAULT_HORIZON_DAYS,
    KIND_NAME,
    READ_SLICE,
    PredictorDeps,
    _aggregate_daily,
    _resolve_window_hours,
    run_method,
)
from legba.data.provenance.models import FindingPayload, PredictionPayload


# ---------------------------------------------------------------------------
# Test doubles — only the LLM boundary
# ---------------------------------------------------------------------------


class _UsageStub:
    def __init__(self, p: int = 50, c: int = 40, r: int = 0) -> None:
        self.prompt_tokens = p
        self.completion_tokens = c
        self.reasoning_tokens = r


class _ResponseStub:
    def __init__(self, content: str, usage: _UsageStub | None = None) -> None:
        self.content = content
        self.usage = usage or _UsageStub()


class _CitingLLMStub:
    """LLM stub that echoes back contributing signal IDs.

    The predictor instructs the LLM to cite signal IDs from the prompt; this
    stub reads the prompt, extracts the first two ``id=`` mentions, and
    returns a narrative that references them as ``signal:<id>``. That makes
    the "cites signals" assertion meaningful without needing a real model.
    """

    subprovider = "stub"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def chat_complete(
        self,
        messages: list[Mapping[str, Any]],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        system: str | None = None,
        **kwargs: Any,
    ) -> _ResponseStub:
        prompt_text = "\n".join(str(m.get("content", "")) for m in messages)
        self.calls.append({"prompt": prompt_text, "system": system})

        # Extract first two signal IDs the prompt offered.
        ids: list[str] = []
        for line in prompt_text.splitlines():
            if "id=" in line:
                start = line.index("id=") + 3
                rest = line[start:].split()
                if rest:
                    ids.append(rest[0])
            if len(ids) >= 2:
                break
        if not ids:
            ids = ["unknown"]
        cites = ", ".join(f"signal:{i}" for i in ids[:2])
        text = (
            "Event volume drifts upward through the observation window, "
            f"driven by {cites}. The forecast extrapolates that trend; "
            "a sudden silencing of those sources would falsify it."
        )
        return _ResponseStub(text)


class _RaisingLLMStub:
    """LLM stub that raises on call — verifies graceful fallback."""

    subprovider = "stub"

    async def chat_complete(self, *args: Any, **kwargs: Any) -> _ResponseStub:
        raise RuntimeError("simulated LLM outage")


# ---------------------------------------------------------------------------
# Synthetic timeseries builder
# ---------------------------------------------------------------------------


def _synthetic_signals(
    *,
    n_days: int = 30,
    base_rate: float = 4.0,
    trend_per_day: float = 0.1,
    end: datetime | None = None,
    target_id: str = "tgt-test",
    seed: int = 11,
) -> list[dict[str, Any]]:
    """Build N days of signals with a mild upward trend.

    Each day has ``round(base_rate + trend_per_day * i + noise)`` signals,
    each carrying a synthetic sentiment value and a per-signal UUID. Result
    is shaped like the rows ``_read_substrate_slice`` returns.
    """
    import random
    rng = random.Random(seed)
    end = end or datetime.now(timezone.utc).replace(microsecond=0)
    rows: list[dict[str, Any]] = []
    for day_idx in range(n_days):
        day = end - timedelta(days=(n_days - 1 - day_idx))
        rate = base_rate + trend_per_day * day_idx
        # Small noise so AutoARIMA has something to chew on without being
        # dominated by noise.
        n = max(0, int(round(rate + rng.gauss(0, 0.6))))
        for k in range(n):
            produced_at = day.replace(hour=k % 24, minute=0, second=0)
            rows.append({
                "id": uuid4(),
                "target_id": target_id,
                "target_version": "v1",
                "source_id": "src-test",
                "source_url": "https://example.test/article",
                "title": f"event on day {day_idx} #{k}",
                "data": {
                    "sentiment": rng.uniform(-0.5, 0.5),
                    "summary": f"synthetic event {day_idx}/{k}",
                },
                "language": "en",
                "produced_at": produced_at,
                "derived_from": [],
            })
    return rows


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_kind_name_is_predictor() -> None:
    assert KIND_NAME == "predictor"


async def test_forecast_is_roughly_continuous_with_history() -> None:
    """30 days of upward-drifting event counts → forecast is finite,
    non-negative, and lands in the same order of magnitude as recent obs.
    Continuity, not precision."""
    inputs = _synthetic_signals(n_days=30, base_rate=4.0, trend_per_day=0.15)
    options = {
        "analyst_id": "test.predictor.v1",
        "analyst_version": "1.0.0",
        "run_id": uuid4(),
        "target_id": "tgt-test",
    }
    result = await run_method(inputs, options, deps=PredictorDeps(llm=None))

    finding = result.finding
    assert isinstance(finding, FindingPayload)
    pred = finding.data["prediction"]
    point = float(pred["point_estimate"])
    lo = float(pred["ci_lower"])
    hi = float(pred["ci_upper"])

    # Forecast must be finite + non-negative (event counts).
    assert math.isfinite(point) and point >= 0.0
    assert math.isfinite(lo) and lo >= 0.0
    assert math.isfinite(hi) and hi >= lo
    assert lo <= point <= hi or abs(point - lo) < 1e-6 or abs(point - hi) < 1e-6

    # Continuity check: recent daily counts averaged ~5-9 events; forecast
    # must be within an order of magnitude of that.
    avg_recent = pred["horizon_series"]
    assert len(avg_recent) == DEFAULT_HORIZON_DAYS
    assert all(math.isfinite(v) and v >= 0.0 for v in avg_recent)
    # Generous bounds — AutoARIMA is allowed to disagree with a simple mean.
    observed_total = finding.data["observed_total_events"]
    observed_days = finding.data["observed_days"]
    obs_mean = observed_total / max(observed_days, 1)
    assert point <= obs_mean * 10.0, (
        f"forecast {point} blew up vs observed mean {obs_mean}"
    )

    # Horizon + CI level surface on the payload exactly.
    assert pred["horizon_days"] == DEFAULT_HORIZON_DAYS
    assert pred["ci_level"] == DEFAULT_CI_LEVEL
    assert pred["method"] in ("auto_arima", "naive_mean")

    # Contributing signal IDs make it through (cap at 200).
    contrib = pred["contributing_signal_ids"]
    assert len(contrib) > 0
    assert all(isinstance(x, str) for x in contrib)
    # They should also appear on the finding's evidence list (cap 50).
    assert len(finding.evidence) > 0
    assert set(finding.evidence).issubset(set(contrib))


async def test_narrative_cites_signal_ids_when_llm_present() -> None:
    inputs = _synthetic_signals(n_days=15, base_rate=3.0)
    options = {
        "analyst_id": "test.predictor.v1",
        "analyst_version": "1.0.0",
        "run_id": uuid4(),
        "target_id": "tgt-A",
    }
    llm = _CitingLLMStub()
    result = await run_method(inputs, options, deps=PredictorDeps(llm=llm))

    finding = result.finding
    narrative = finding.data["prediction"]["narrative"]
    # The stub cites the first two signal IDs it saw in the prompt.
    assert "signal:" in narrative
    # And those signal IDs must have actually come from the contributing set.
    cited_tokens = [
        token.split("signal:", 1)[1].strip(" .,;:")
        for token in narrative.split()
        if token.startswith("signal:")
    ]
    contrib_ids = set(finding.data["prediction"]["contributing_signal_ids"])
    assert any(c in contrib_ids for c in cited_tokens), (
        f"narrative cites {cited_tokens} but contributing set has none of them"
    )

    # Usage was recorded from the stub.
    assert result.usage.get("prompt_tokens", 0) > 0


async def test_llm_failure_falls_back_to_no_narrative() -> None:
    inputs = _synthetic_signals(n_days=20, base_rate=5.0)
    options = {
        "analyst_id": "test.predictor.v1",
        "analyst_version": "1.0.0",
        "run_id": uuid4(),
        "target_id": "tgt-B",
    }
    result = await run_method(
        inputs, options, deps=PredictorDeps(llm=_RaisingLLMStub()),
    )

    finding = result.finding
    narrative = finding.data["prediction"]["narrative"]
    assert "no narrative available" in narrative.lower()

    # The numeric forecast must still be intact.
    pred = finding.data["prediction"]
    assert math.isfinite(float(pred["point_estimate"]))
    assert pred["method"] in ("auto_arima", "naive_mean")
    # And usage stays empty (LLM never returned).
    assert result.usage == {}


async def test_no_llm_supplied_emits_fallback_narrative() -> None:
    inputs = _synthetic_signals(n_days=10, base_rate=2.0)
    options = {
        "analyst_id": "test.predictor.v1",
        "analyst_version": "1.0.0",
        "run_id": uuid4(),
        "target_id": "tgt-C",
    }
    result = await run_method(inputs, options, deps=PredictorDeps(llm=None))
    narrative = result.finding.data["prediction"]["narrative"]
    assert "no narrative available" in narrative.lower()


async def test_empty_inputs_produce_noop_finding() -> None:
    options = {
        "analyst_id": "test.predictor.v1",
        "analyst_version": "1.0.0",
        "run_id": uuid4(),
        "target_id": "tgt-empty",
    }
    result = await run_method([], options, deps=PredictorDeps(llm=None))
    finding = result.finding
    assert finding.confidence == 0.0
    assert "noop" in finding.tags
    assert finding.data["forecast_method"] == "none"
    # The structured prediction payload is still present for callers.
    pred = PredictionPayload.model_validate(finding.data["prediction"])
    assert pred.confidence == 0.0


async def test_undated_inputs_are_treated_as_noop() -> None:
    # Rows missing produced_at are skipped by the aggregator; if every row
    # is undated the result is the same as empty.
    rows = [
        {
            "id": uuid4(),
            "target_id": "tgt-u",
            "title": "no date here",
            "data": {},
            "produced_at": None,
        }
        for _ in range(5)
    ]
    options = {
        "analyst_id": "test.predictor.v1",
        "analyst_version": "1.0.0",
        "run_id": uuid4(),
        "target_id": "tgt-u",
    }
    result = await run_method(rows, options, deps=PredictorDeps(llm=None))
    assert "noop" in result.finding.tags


async def test_short_series_uses_naive_fallback() -> None:
    """3-day input → too short for AutoARIMA, falls back to naive_mean."""
    inputs = _synthetic_signals(n_days=3, base_rate=2.0, trend_per_day=0.0)
    options = {
        "analyst_id": "test.predictor.v1",
        "analyst_version": "1.0.0",
        "run_id": uuid4(),
        "target_id": "tgt-short",
    }
    result = await run_method(inputs, options, deps=PredictorDeps(llm=None))
    pred = result.finding.data["prediction"]
    assert pred["method"] == "naive_mean"
    # Naive forecast emits a flat horizon series at the mean.
    assert len(pred["horizon_series"]) == DEFAULT_HORIZON_DAYS
    distinct = set(round(v, 4) for v in pred["horizon_series"])
    assert len(distinct) == 1


async def test_custom_horizon_and_ci_level() -> None:
    inputs = _synthetic_signals(n_days=20, base_rate=3.0)
    options = {
        "analyst_id": "test.predictor.v1",
        "analyst_version": "1.0.0",
        "run_id": uuid4(),
        "target_id": "tgt-cfg",
    }
    deps = PredictorDeps(llm=None, horizon_days=14, ci_level=95)
    result = await run_method(inputs, options, deps=deps)
    pred = result.finding.data["prediction"]
    assert pred["horizon_days"] == 14
    assert pred["ci_level"] == 95
    assert len(pred["horizon_series"]) == 14


async def test_prediction_payload_round_trips_through_model() -> None:
    """The structured prediction blob must validate against PredictionPayload
    (extras allowed). This guards against silent shape drift."""
    inputs = _synthetic_signals(n_days=12, base_rate=3.0)
    options = {
        "analyst_id": "test.predictor.v1",
        "analyst_version": "1.0.0",
        "run_id": uuid4(),
        "target_id": "tgt-D",
    }
    result = await run_method(inputs, options, deps=PredictorDeps(llm=None))
    blob = result.finding.data["prediction"]
    parsed = PredictionPayload.model_validate(blob)
    assert parsed.source_type == "stat_forecast"
    assert parsed.category == "event_count_forecast"
    assert 0.0 <= parsed.confidence <= 1.0
    # Extras-allowed fields survive the round-trip via model_extra.
    extras = parsed.model_extra or {}
    assert "point_estimate" in extras
    assert "ci_lower" in extras and "ci_upper" in extras


# ---------------------------------------------------------------------------
# DQ-H1(b): daily aggregation over the full window (not the 50-row cap)
# ---------------------------------------------------------------------------


def test_aggregate_daily_honours_preaggregated_count() -> None:
    """The custom READ_SLICE delivers one bucket per day with an explicit
    ``count`` + ``sample_ids``; _aggregate_daily must use the count (not treat
    the bucket as a single signal) and lift the sample ids."""
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = [
        {"produced_at": (base + timedelta(days=0)).isoformat(), "count": 2380,
         "sample_ids": ["a1", "a2"], "id": "a1"},
        {"produced_at": (base + timedelta(days=1)).isoformat(), "count": 2410,
         "sample_ids": ["b1"], "id": "b1"},
        {"produced_at": (base + timedelta(days=2)).isoformat(), "count": 2350,
         "sample_ids": ["c1", "c2", "c3"], "id": "c1"},
    ]
    series = _aggregate_daily(rows)
    assert series is not None
    # Counts reflect the real daily volume, NOT 1-per-bucket.
    assert series.counts.tolist() == [2380.0, 2410.0, 2350.0]
    # Sample ids are lifted for citation.
    assert "a1" in series.signal_ids and "c3" in series.signal_ids


def test_aggregate_daily_backward_compatible_raw_rows() -> None:
    """Raw signal rows (no ``count``) still count as 1 each — the default
    reader + existing tests must not regress."""
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = [
        {"produced_at": base.isoformat(), "id": "x1"},
        {"produced_at": base.isoformat(), "id": "x2"},
        {"produced_at": (base + timedelta(days=1)).isoformat(), "id": "x3"},
    ]
    series = _aggregate_daily(rows)
    assert series is not None
    assert series.counts.tolist() == [2.0, 1.0]


def test_resolve_window_hours_parses_and_defaults() -> None:
    class _Targets:
        time_window = "336h"

    class _Sub:
        targets = _Targets()

    class _Desc:
        subscription = _Sub()

    assert _resolve_window_hours(_Desc()) == 336

    class _DescNone:
        subscription = None

    assert _resolve_window_hours(_DescNone()) == DEFAULT_FORECAST_WINDOW_HOURS


class _FakeConn:
    """Minimal asyncpg-conn double for the predictor READ_SLICE."""

    def __init__(self, target_body: dict | None, daily_rows: list[dict]) -> None:
        self._target_body = target_body
        self._daily_rows = daily_rows

    async def fetchrow(self, _query: str, *_args: Any):
        if self._target_body is None:
            return None
        return {"body": self._target_body}

    async def fetch(self, _query: str, *_args: Any):
        return self._daily_rows


class _FakeDesc:
    class subscription:  # noqa: N801
        class targets:  # noqa: N801
            time_window = "720h"


async def test_read_slice_shapes_daily_buckets() -> None:
    """READ_SLICE returns one daily-bucket row per DB group, carrying count +
    sample_ids, so a high-volume target gets a real multi-day series."""
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    daily = [
        {"day": base, "cnt": 2380, "sample_ids": ["a1", "a2"]},
        {"day": base + timedelta(days=1), "cnt": 2410, "sample_ids": ["b1"]},
    ]
    conn = _FakeConn(target_body={"sources": [], "scope": {"geo": ["US"]}}, daily_rows=daily)
    rows = await READ_SLICE(conn, descriptor=_FakeDesc(), target_filter="usa_news")
    assert len(rows) == 2
    assert rows[0]["count"] == 2380
    assert rows[0]["sample_ids"] == ["a1", "a2"]
    # And the series built from it is non-degenerate.
    series = _aggregate_daily(rows)
    assert series is not None
    assert series.counts.tolist() == [2380.0, 2410.0]
