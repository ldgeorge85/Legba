# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Alert kind ``search_degraded``."""

from legba.data.type_registry import declare


TYPE = declare(
    family="alert_kind",
    id="search_degraded",
    position=260,
    subject_kind="platform",
    lifecycle="condition",
    quiet_close_hours=None,
    group="analyst_failure",
    topic="engine",
    paging="page_rule",
    doc="The search plane's NOT_FOUND share is degraded.",
)
