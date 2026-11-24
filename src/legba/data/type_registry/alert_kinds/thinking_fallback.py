# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Alert kind ``thinking_fallback``.

Topic: T-2: a thinking call fell back to the plain alias
(data/stack/llm/thinking.py). Logged only: the shipped page rule has no
entry for it; paging it is a new rule version (the owner's call).
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="alert_kind",
    id="thinking_fallback",
    position=275,
    subject_kind="route",
    lifecycle="event",
    quiet_close_hours=24,
    group="analyst_failure",
    topic="engine",
    paging="logged_only",
    doc="A thinking call failed for a reason the thinking alias causes and was retried once on the plain alias (logged only, never paged by default).",
)
