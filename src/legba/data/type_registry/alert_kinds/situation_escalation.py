# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Alert kind ``situation_escalation``."""

from legba.data.type_registry import declare


TYPE = declare(
    family="alert_kind",
    id="situation_escalation",
    position=40,
    subject_kind="target",
    lifecycle="event",
    quiet_close_hours=24,
    group="findings",
    topic="world",
    paging="page_rule",
    doc='A tracked situation escalated.',
)
