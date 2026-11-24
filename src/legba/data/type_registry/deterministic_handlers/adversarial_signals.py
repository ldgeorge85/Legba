# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic handler ``adversarial_signals``.

Output: Adversarial-signal scoring. A genuine finding.

Dispatch: L-203 migrated maintenance modules
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="deterministic_handler",
    id="adversarial_signals",
    implementation="legba.data.analysts.deterministic_handlers.adversarial_signals:handle",
    output="finding",
    doc="Adversarial-signal scoring over recent signals.",
)
