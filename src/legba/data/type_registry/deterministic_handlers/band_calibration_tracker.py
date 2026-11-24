# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic handler ``band_calibration_tracker``.

Output: P2-3 band-calibration harness — band transitions logged as
resolvable claims (band_calibration_claims side-writes, migration 0093) +
the per-run persistence/reversal aggregate. The summary IS the measurement
product the /eval/calibration band_calibration section reads (the
calibration_tracking precedent), so it stays a genuine FINDING — and its
data carries the explicit no-Brier honesty note (bands are not
probabilities).

Dispatch: P2-3 band-calibration harness — logs a resolvable claim per
scorecard band transition (ladder→ladder only), auto-resolves each at
T0+14d/28d against LATER scorecard rows (deterministic, no LLM), and emits
the honest persistence/reversal summary finding the /eval/calibration band
section projects. Watermark + unique-index dedup make claim logging fire-
once.
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="deterministic_handler",
    id="band_calibration_tracker",
    implementation="legba.data.analysts.deterministic_handlers.band_calibration_tracker:handle",
    output="finding",
    requires_pool=True,
    doc="Scorecard band transitions logged as resolvable claims and resolved against later scorecard rows.",
)
