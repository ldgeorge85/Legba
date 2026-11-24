# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic handler ``forecast_scoreboard``.

Output: P4-T7 acute-forecast scoreboard producer — the REAL product = the
side-written `acute_forecasts` rows (issued / exogenously resolved by the
forecast_acute writers this handler DRIVES); the returned summary is a per-
run counts RECEIPT fully audited in analyst_traces. TRACE_ONLY so the
receipt NEVER lands a finding / prediction / claim on any trust surface —
forecasting surfaces ONLY as acute_forecasts rows + the T4 scoreboard.

Dispatch: P4-T7 acute-forecast scoreboard producer — weekly-idempotent
driver for the forecast_acute pilot (issue → exogenous-resolve → count).
Side-writes the acute_forecasts rows via the existing forecast_acute
writers; returns a TRACE_ONLY counts receipt. Forecasting surfaces only in
the T4 scoreboard.
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="deterministic_handler",
    id="forecast_scoreboard",
    implementation="legba.data.analysts.deterministic_handlers.forecast_scoreboard:handle",
    output="trace_only",
    doc="Drives the acute-forecast pilot: issue, resolve, count.",
)
