# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Alert kind ``forecast_resolution``."""

from legba.data.type_registry import declare


TYPE = declare(
    family="alert_kind",
    id="forecast_resolution",
    position=310,
    subject_kind="question",
    lifecycle="event",
    quiet_close_hours=24,
    group="forecast",
    topic="engine",
    paging="page_rule",
    doc='A forecast question has a new resolution proposal or waits on a judgement.',
)
