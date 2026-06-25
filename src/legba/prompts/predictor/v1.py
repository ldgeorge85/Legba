# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""L-174 ``predictor`` DSPy prompt module — v1.

Per L-105 §2.3.  Wraps the OPTIONAL narrative LLM call only.

The predictor's statistical core (daily-aggregated AutoARIMA from
statsforecast, plus a naive-mean fallback for series shorter than
``MIN_OBSERVATIONS``) stays in pure Python — see
:mod:`legba.data.analysts.predictor._fit_forecast`.  The numeric forecast
+ confidence intervals are computed deterministically; the LLM's only
job is to write the explanatory paragraph that cites the load-bearing
signals.

When no narrative LLM is supplied (``PredictorDeps.llm is None``), the
kind handler skips this module and emits the fixed terse narrative
"no narrative available — stat-model output only".  The Signature here
matches the prompt shape constructed by
:func:`legba.data.analysts.predictor._render_narrative_prompt`.
"""

from __future__ import annotations

from typing import Any

import dspy

__all__ = [
    "ForecastNarrativeSignature",
    "PredictorNarrative",
    "build",
]


class ForecastNarrativeSignature(dspy.Signature):
    """Explanatory narrative for a stat-model forecast.

    Inputs:
        target_id              — the target this forecast belongs to.
        observed_window        — ISO date range for the observed window
                                  (e.g. "2026-05-07 -> 2026-05-20").
        daily_counts           — comma-separated string of daily event
                                  counts for the observed window.
        forecast_method        — name of the forecaster (e.g.
                                  "autoarima" or "naive_mean_fallback").
        forecast_horizon_days  — int — operator-week horizon (default 7).
        point_estimate         — float — final-step point estimate.
        ci_low                 — float — lower CI bound.
        ci_high                — float — upper CI bound.
        ci_level               — int — CI percentage (default 80).
        recent_signals_block   — pre-rendered block of recent signals
                                  with id, produced_at, title (capped at
                                  MAX_INPUT_SIGNALS_FOR_NARRATIVE).

    Outputs:
        narrative              — 3-5 sentences explaining the forecast.
                                  References signal IDs inline as
                                  ``signal:<id>``.  Must not invent IDs.
    """

    target_id: str = dspy.InputField(desc="Target id (or 'unspecified')")
    observed_window: str = dspy.InputField(
        desc="ISO date range of the observed window",
    )
    daily_counts: str = dspy.InputField(
        desc="Comma-separated daily event counts",
    )
    forecast_method: str = dspy.InputField(
        desc="Forecaster name (e.g. autoarima / naive_mean_fallback)",
    )
    forecast_horizon_days: int = dspy.InputField(
        desc="Forecast horizon in days",
    )
    point_estimate: float = dspy.InputField(
        desc="Point estimate at the final forecast step",
    )
    ci_low: float = dspy.InputField(desc="Lower CI bound")
    ci_high: float = dspy.InputField(desc="Upper CI bound")
    ci_level: int = dspy.InputField(desc="CI percentage (e.g. 80)")
    recent_signals_block: str = dspy.InputField(
        desc="Pre-rendered recent-signals block — one line per signal "
             "with id, produced_at, title",
    )

    narrative: str = dspy.OutputField(
        desc="3-5 sentences explaining the forecast.  Reference signals "
             "inline as 'signal:<id>'.  Cite only IDs from the block.",
    )


class PredictorNarrative(dspy.Module):
    """Optional narrative wrapper for the predictor's stat output.

    Single-call ChainOfThought so the optimizer (L-176) can compile
    candidate narratives against the trace set without touching the
    stat core (which is deterministic anyway).
    """

    def __init__(self) -> None:
        super().__init__()
        self.narrate = dspy.ChainOfThought(ForecastNarrativeSignature)

    def forward(  # type: ignore[override]
        self,
        target_id: str,
        observed_window: str,
        daily_counts: str,
        forecast_method: str,
        forecast_horizon_days: int,
        point_estimate: float,
        ci_low: float,
        ci_high: float,
        ci_level: int,
        recent_signals_block: str,
    ) -> Any:
        return self.narrate(
            target_id=target_id,
            observed_window=observed_window,
            daily_counts=daily_counts,
            forecast_method=forecast_method,
            forecast_horizon_days=forecast_horizon_days,
            point_estimate=point_estimate,
            ci_low=ci_low,
            ci_high=ci_high,
            ci_level=ci_level,
            recent_signals_block=recent_signals_block,
        )


def build() -> PredictorNarrative:
    """Return a fresh :class:`PredictorNarrative` instance."""
    return PredictorNarrative()
