# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Alert kind ``escalations``.

Topic: the escalate pack's channel.
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="alert_kind",
    id="escalations",
    position=10,
    subject_kind="target",
    lifecycle="event",
    quiet_close_hours=24,
    group="findings",
    topic="world",
    paging="page_rule",
    doc="A desk's agent escalated a finding (a transition, not a level).",
)
