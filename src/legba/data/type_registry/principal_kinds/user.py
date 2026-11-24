# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Principal kind ``user``."""

from legba.data.type_registry import declare


TYPE = declare(
    family="principal_kind",
    id="user",
    name_shape="the person's users.id",
    example="user:0b6c2f0e-1111-4c2a-9d3e-000000000001",
    doc="A signed-in person, linked to their users row by id; each person is their own principal, whatever their role.",
)
