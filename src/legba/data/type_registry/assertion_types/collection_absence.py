# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Assertion type ``collection_absence``."""

from legba.data.type_registry import declare


TYPE = declare(
    family="assertion_type",
    id="collection_absence",
    assertion_class="absence",
    key=('topic', 'collection'),
    desk_row_kinds=('absence',),
    evidence="background",
    doc="Nothing matching a topic was collected from a desk's sources in a window: a record about the collection, never about the world.",
)
