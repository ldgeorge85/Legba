# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Alert kind ``contention_flip``.

Topic: kill-listed (default off)
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="alert_kind",
    id="contention_flip",
    position=70,
    subject_kind="target",
    lifecycle="event",
    quiet_close_hours=24,
    group="findings",
    topic="world",
    paging="page_rule",
    doc='A contested claim flipped (kill-listed by default).',
)
