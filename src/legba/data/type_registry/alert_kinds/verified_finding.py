# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Alert kind ``verified_finding``."""

from legba.data.type_registry import declare


TYPE = declare(
    family="alert_kind",
    id="verified_finding",
    position=20,
    subject_kind="target",
    lifecycle="event",
    quiet_close_hours=24,
    group="findings",
    topic="world",
    paging="page_rule",
    doc='A verified high-severity finding.',
)
