# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Assertion type ``judgement``."""

from legba.data.type_registry import declare


TYPE = declare(
    family="assertion_type",
    id="judgement",
    assertion_class="assessment",
    key=('text',),
    desk_row_kinds=('assessment',),
    evidence="background",
    doc="A desk's judgement with its confidence word, resting on held statements as premises; never established.",
)
