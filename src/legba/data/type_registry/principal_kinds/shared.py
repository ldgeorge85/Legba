# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Principal kind ``shared``."""

from legba.data.type_registry import declare


TYPE = declare(
    family="principal_kind",
    id="shared",
    name_shape="admin-token (the shared admin credential) or dev (a development-mode registry with no credential)",
    example="shared:admin-token",
    doc="A credential several people may hold; a write under it names the credential, never a person (with sign-in off every person writes as shared:admin-token).",
)
