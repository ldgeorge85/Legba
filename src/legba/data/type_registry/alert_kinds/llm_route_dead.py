# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Alert kind ``llm_route_dead``."""

from legba.data.type_registry import declare


TYPE = declare(
    family="alert_kind",
    id="llm_route_dead",
    position=240,
    subject_kind="route",
    lifecycle="condition",
    quiet_close_hours=None,
    group="analyst_failure",
    topic="engine",
    paging="page_rule",
    doc='A busy LLM route answered almost nothing over 24 hours.',
)
