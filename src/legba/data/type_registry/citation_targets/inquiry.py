# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Citation target ``inquiry``."""

from legba.data.type_registry import declare


TYPE = declare(
    family="citation_target",
    id="inquiry",
    table="inquiry_ledger",
    column="inquiry_id",
    ref_kinds=("inquiry_ledger",),
    evidence="background",
    doc="An inquiry-ledger entry, cited by id.",
)
