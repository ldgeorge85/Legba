# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Alert kind ``external_audit``."""

from legba.data.type_registry import declare


TYPE = declare(
    family="alert_kind",
    id="external_audit",
    position=230,
    subject_kind="target",
    lifecycle="event",
    quiet_close_hours=24,
    group="analyst_failure",
    topic="engine",
    paging="page_rule",
    doc='An external web check contradicted a sampled high-severity claim.',
)
