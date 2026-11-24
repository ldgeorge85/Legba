# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Principal kind ``agent``."""

from legba.data.type_registry import declare


TYPE = declare(
    family="principal_kind",
    id="agent",
    name_shape="the analyst descriptor id, @, the descriptor version",
    example="agent:desk_northaven@3f9a0c",
    doc="An analyst seat that calls a model; one principal per descriptor version, so a changed prompt is a different writer.",
)
