# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Alert kind ``host_search_canary``."""

from legba.data.type_registry import declare


TYPE = declare(
    family="alert_kind",
    id="host_search_canary",
    position=350,
    subject_kind="host",
    lifecycle="event",
    quiet_close_hours=6,
    group="host",
    topic="engine",
    paging="page_rule",
    doc="The host's search canary failed consecutive probes.",
)
