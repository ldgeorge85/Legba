# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Alert kind ``production_deficit``."""

from legba.data.type_registry import declare


TYPE = declare(
    family="alert_kind",
    id="production_deficit",
    position=210,
    subject_kind="analyst",
    lifecycle="event",
    quiet_close_hours=24,
    group="analyst_failure",
    topic="engine",
    paging="page_rule",
    doc='A loop produced less than its declared output.',
)
