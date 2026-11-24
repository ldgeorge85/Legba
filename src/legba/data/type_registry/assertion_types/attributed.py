# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Assertion type ``attributed``."""

from legba.data.type_registry import declare


TYPE = declare(
    family="assertion_type",
    id="attributed",
    assertion_class="attributed",
    key=('text',),
    desk_row_kinds=('attributed',),
    evidence="background",
    doc="What a named speaker said ('X said Y'): it holds that it was said, never that it is so.",
)
