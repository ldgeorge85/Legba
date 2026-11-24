# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Alert kind ``llm_route_auth_failed``."""

from legba.data.type_registry import declare


TYPE = declare(
    family="alert_kind",
    id="llm_route_auth_failed",
    position=250,
    subject_kind="route",
    lifecycle="condition",
    quiet_close_hours=None,
    group="analyst_failure",
    topic="engine",
    paging="page_rule",
    doc='An LLM route refuses authentication (401/402/403/404).',
)
