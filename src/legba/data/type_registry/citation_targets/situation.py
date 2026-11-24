# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Citation target ``situation``."""

from legba.data.type_registry import declare


TYPE = declare(
    family="citation_target",
    id="situation",
    table="situations",
    column="situation_id",
    ref_kinds=("situation",),
    member_fields={"situation_register": "situation_ids"},
    evidence="background",
    doc="An open situation, cited by id or as a member of the open-situation register block.",
)
