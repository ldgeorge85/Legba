# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Alert kind ``volume_surge``.

Topic: a subject's signal count far above its baseline
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="alert_kind",
    id="volume_surge",
    position=100,
    subject_kind="target",
    lifecycle="condition",
    quiet_close_hours=2,
    group="baseline",
    topic="world",
    paging="page_rule",
    doc="A subject's signal count over the last hour is far above its own baseline and comes from several independent outlets.",
)
