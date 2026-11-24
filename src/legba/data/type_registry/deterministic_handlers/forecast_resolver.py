# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic handler ``forecast_resolver``.

Output: ST1 forecast_resolver — the REAL product is the side-written
forecast_resolution_records proposals (migration 0244); the returned summary
is a per-run counts receipt.

Dispatch: ST1 forecast_resolver — every 6 h: due sealed forecast questions
get a rule's PROPOSAL (no model), series_threshold settles by rule, every
new proposal / needed judgement raises one alert. The operator confirms or
overrides the rest (scripts/resolve_forecasts.py).
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="deterministic_handler",
    id="forecast_resolver",
    implementation="legba.data.analysts.deterministic_handlers.forecast_resolver:handle",
    output="trace_only",
    alerts=("forecast_resolution",),
    doc="Proposes resolutions for due sealed forecast questions; settles series thresholds by rule.",
)
