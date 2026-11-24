# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Alert kind ``analyst_hard_fail``."""

from legba.data.type_registry import declare


TYPE = declare(
    family="alert_kind",
    id="analyst_hard_fail",
    position=200,
    subject_kind="analyst",
    lifecycle="event",
    quiet_close_hours=6,
    group="analyst_failure",
    topic="engine",
    paging="page_rule",
    doc='An analyst run failed hard under retry.hard.strategy=dlq_and_alert.',
)
