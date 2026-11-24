# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Alert kind ``geo_convergence``.

Topic: kill-listed (default off)
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="alert_kind",
    id="geo_convergence",
    position=80,
    subject_kind="target",
    lifecycle="event",
    quiet_close_hours=24,
    group="findings",
    topic="world",
    paging="page_rule",
    doc='Several sources converged on one place (kill-listed by default).',
)
