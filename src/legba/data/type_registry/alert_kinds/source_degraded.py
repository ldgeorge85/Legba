# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Alert kind ``source_degraded``."""

from legba.data.type_registry import declare


TYPE = declare(
    family="alert_kind",
    id="source_degraded",
    position=130,
    subject_kind="source",
    lifecycle="condition",
    quiet_close_hours=None,
    group="source_health",
    topic="engine",
    paging="page_rule",
    doc="A source's polls come back empty while upstream has newer entries.",
)
