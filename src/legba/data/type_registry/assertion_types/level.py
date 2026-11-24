# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Assertion type ``level``."""

from legba.data.type_registry import declare


TYPE = declare(
    family="assertion_type",
    id="level",
    assertion_class="statement",
    key=('text',),
    desk_row_kinds=('level',),
    evidence="background",
    doc="A figure about the subject (one figure per record), checked against the quotes it rests on.",
)
