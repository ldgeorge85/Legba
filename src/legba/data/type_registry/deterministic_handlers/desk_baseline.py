# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic handler ``desk_baseline``.

Output: A7 P3-7 CAST-recipe per-desk statistical baseline — daily recompute
of the per-desk (g20 + watch) trailing baseline expectation + uncertainty
band + current-window deviation into desk_baselines (migration 0103). The
honest per-desk distribution IS the measurement product (the
calibration_tracking / fact_decay_scan / source_track_record precedent), so
it stays a genuine FINDING — which keeps this handler in the FINDING-
emitters set the STRUCTURAL_VERIFY_EXEMPT drift guard asserts. NOT a
forecast (frozen): no Brier, no skill, no prediction-as-claim — a
falsifiable prior the desk LLM reads + the P1-3 baseline_deviation trigger
can consume, never a forecast.

Dispatch: A7 P3-7 CAST-recipe per-desk statistical baseline — daily LLM-free
recompute over the g20 + watch desk set. For each desk × {signal_volume,
high_sev_findings} it computes a robust trailing baseline (mean rate,
median, Poisson-floored sigma) + an uncertainty band + the current-window
deviation (with the SAME absolute floors as the P1-3 trigger), plus the CAST
feature recipe (lags 1/7/28, rolling means, time-since-last-high-sev,
neighbour-desk spillover), into desk_baselines (0103). Returns an honest
distribution FINDING; NEVER a forecast (no Brier / skill / prediction).
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="deterministic_handler",
    id="desk_baseline",
    implementation="legba.data.analysts.deterministic_handlers.desk_baseline:handle",
    output="finding",
    requires_pool=True,
    doc="Per-desk statistical baseline and the current window's deviation.",
)
