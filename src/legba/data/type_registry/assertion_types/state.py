# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Assertion type ``state``."""

from legba.data.type_registry import declare


TYPE = declare(
    family="assertion_type",
    id="state",
    assertion_class="statement",
    key=('text',),
    desk_row_kinds=('state',),
    evidence="background",
    doc="A condition of the subject that holds over a period, checked against the quotes it rests on.",
)
