# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Alert kind ``analyst_cursor_lag``.

Topic: N-Z: an analyst's resume cursor behind real time
(runtime/analyst_cursor_lag.py).
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="alert_kind",
    id="analyst_cursor_lag",
    position=190,
    subject_kind="analyst",
    lifecycle="condition",
    quiet_close_hours=None,
    group="analyst_failure",
    topic="engine",
    paging="page_rule",
    doc="An analyst's cursor is further behind real time than its declared lag.",
)
