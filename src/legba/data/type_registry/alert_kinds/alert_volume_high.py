# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Alert kind ``alert_volume_high``."""

from legba.data.type_registry import declare


TYPE = declare(
    family="alert_kind",
    id="alert_volume_high",
    position=290,
    subject_kind="platform",
    lifecycle="condition",
    quiet_close_hours=None,
    group="pipeline",
    topic="engine",
    paging="page_rule",
    doc='Pushes on one topic over 24 hours exceed the declared cap.',
)
