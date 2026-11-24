# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Alert kind ``analyst_alert_output``.

Topic: the first-generation ``alert`` output kind
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="alert_kind",
    id="analyst_alert_output",
    position=110,
    subject_kind="target",
    lifecycle="event",
    quiet_close_hours=24,
    group="findings",
    topic="world",
    paging="logged_only",
    doc="An analyst's first-generation 'alert' output fired.",
)
