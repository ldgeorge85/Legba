# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Alert kind ``reminder_orphan_collected``."""

from legba.data.type_registry import declare


TYPE = declare(
    family="alert_kind",
    id="reminder_orphan_collected",
    position=300,
    subject_kind="platform",
    lifecycle="event",
    quiet_close_hours=24,
    group="pipeline",
    topic="engine",
    paging="logged_only",
    doc='An orphan Dapr reminder was collected.',
)
