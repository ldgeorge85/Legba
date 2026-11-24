# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Alert kind ``coverage_floor``."""

from legba.data.type_registry import declare


TYPE = declare(
    family="alert_kind",
    id="coverage_floor",
    position=220,
    subject_kind="target",
    lifecycle="event",
    quiet_close_hours=24,
    group="analyst_failure",
    topic="engine",
    paging="page_rule",
    doc="A desk's coverage fell under its floor.",
)
