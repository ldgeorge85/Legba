# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Alert kind ``watchlist_hit``."""

from legba.data.type_registry import declare


TYPE = declare(
    family="alert_kind",
    id="watchlist_hit",
    position=60,
    subject_kind="target",
    lifecycle="event",
    quiet_close_hours=24,
    group="findings",
    topic="world",
    paging="page_rule",
    doc='An operator watchlist entry matched.',
)
