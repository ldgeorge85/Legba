# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Alert kind ``rollup``.

Topic: the per-desk cap's rollup goes to world when any member does
(``alerts.topics.topic_for_rollup``).
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="alert_kind",
    id="rollup",
    position=90,
    subject_kind="target",
    lifecycle="event",
    quiet_close_hours=24,
    group="findings",
    topic="world",
    paging="page_rule",
    doc='The per-desk cap folded several trigger alerts into one.',
)
