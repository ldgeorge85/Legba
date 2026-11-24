# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Alert kind ``nightly_suite``."""

from legba.data.type_registry import declare


TYPE = declare(
    family="alert_kind",
    id="nightly_suite",
    position=370,
    subject_kind="deploy",
    lifecycle="event",
    quiet_close_hours=24,
    group="deploy",
    topic="nightly",
    paging="page_rule",
    doc="The nightly test suite's verdict.",
)
