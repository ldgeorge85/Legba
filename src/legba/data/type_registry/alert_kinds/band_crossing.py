# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Alert kind ``band_crossing``.

Topic: batched: one push per tick
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="alert_kind",
    id="band_crossing",
    position=30,
    subject_kind="target",
    lifecycle="event",
    quiet_close_hours=24,
    group="findings",
    topic="world",
    paging="page_rule",
    doc='A desk scorecard band moved (one push per trigger tick).',
)
