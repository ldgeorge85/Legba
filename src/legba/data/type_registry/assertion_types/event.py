# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Assertion type ``event``."""

from legba.data.type_registry import declare


TYPE = declare(
    family="assertion_type",
    id="event",
    assertion_class="statement",
    key=('text',),
    desk_row_kinds=('event',),
    evidence="background",
    doc="Something that happened to the subject at a time, checked against the quotes it rests on.",
)
