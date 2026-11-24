# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Alert kind ``baseline_deviation``."""

from legba.data.type_registry import declare


TYPE = declare(
    family="alert_kind",
    id="baseline_deviation",
    position=50,
    subject_kind="target",
    lifecycle="event",
    quiet_close_hours=24,
    group="baseline",
    topic="world",
    paging="logged_only",
    doc="A desk's daily signal or high-severity-finding count left its baseline band.",
)
