# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Alert kind ``host_disk``."""

from legba.data.type_registry import declare


TYPE = declare(
    family="alert_kind",
    id="host_disk",
    position=330,
    subject_kind="host",
    lifecycle="condition",
    quiet_close_hours=None,
    group="host",
    topic="engine",
    paging="page_rule",
    doc='The root filesystem entered a usage band (df), or left one.',
)
