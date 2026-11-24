# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Alert kind ``source_staleness_stale``."""

from legba.data.type_registry import declare


TYPE = declare(
    family="alert_kind",
    id="source_staleness_stale",
    position=140,
    subject_kind="source",
    lifecycle="condition",
    quiet_close_hours=None,
    group="source_health",
    topic="engine",
    paging="page_rule",
    doc="A source's newest signal is stale for its grade.",
)
