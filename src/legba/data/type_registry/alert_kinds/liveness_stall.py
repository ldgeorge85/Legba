# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Alert kind ``liveness_stall``."""

from legba.data.type_registry import declare


TYPE = declare(
    family="alert_kind",
    id="liveness_stall",
    position=280,
    subject_kind="platform",
    lifecycle="event",
    quiet_close_hours=1,
    group="pipeline",
    topic="engine",
    paging="page_rule",
    doc='No signal and no finding for the stall window.',
)
