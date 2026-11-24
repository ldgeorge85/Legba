# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic handler ``calibration_tracking``.

Output: Confidence-vs-outcome Brier/reliability tracking. A genuine finding.
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="deterministic_handler",
    id="calibration_tracking",
    implementation="legba.data.analysts.deterministic_handlers.calibration_tracking:handle",
    output="finding",
    doc="Analyst confidence against outcome: Brier score, reliability bins and drift across windows.",
)
