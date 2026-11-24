# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Alert kind ``host_stall_recovered``."""

from legba.data.type_registry import declare


TYPE = declare(
    family="alert_kind",
    id="host_stall_recovered",
    position=360,
    subject_kind="host",
    lifecycle="event",
    quiet_close_hours=24,
    group="host",
    topic="engine",
    paging="logged_only",
    doc='The host stall watchdog restarted the plane after a stall.',
)
