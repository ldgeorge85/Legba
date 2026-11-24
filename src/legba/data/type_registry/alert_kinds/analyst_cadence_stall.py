# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Alert kind ``analyst_cadence_stall``."""

from legba.data.type_registry import declare


TYPE = declare(
    family="alert_kind",
    id="analyst_cadence_stall",
    position=180,
    subject_kind="analyst",
    lifecycle="condition",
    quiet_close_hours=None,
    group="analyst_failure",
    topic="engine",
    paging="page_rule",
    doc="An analyst's newest successful run is older than twice its interval.",
)
