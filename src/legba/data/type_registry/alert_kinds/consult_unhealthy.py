# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Alert kind ``consult_unhealthy``.

Topic: N consecutive failed consult turns
(runtime/consult_health.py).
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="alert_kind",
    id="consult_unhealthy",
    position=270,
    subject_kind="route",
    lifecycle="condition",
    quiet_close_hours=None,
    group="consult_failure",
    topic="engine",
    paging="page_rule",
    doc='Several consecutive consult turns failed or did not finish.',
)
