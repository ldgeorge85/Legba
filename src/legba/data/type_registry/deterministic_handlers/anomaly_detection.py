# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic handler ``anomaly_detection``.

Output: Volume/sentiment/novel-entity anomaly scores. A genuine finding.
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="deterministic_handler",
    id="anomaly_detection",
    implementation="legba.data.analysts.deterministic_handlers.anomaly_detection:handle",
    output="finding",
    doc="Signal-volume rate spikes, sentiment-shift z-scores and novel-entity emergence over recent windows.",
)
