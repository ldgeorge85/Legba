# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Citation target ``signal``."""

from legba.data.type_registry import declare


TYPE = declare(
    family="citation_target",
    id="signal",
    table="signals",
    column="signal_id",
    material=True,
    ref_kinds=("signal",),
    id_fields=("signal_id", "ref_id"),
    evidence="evidence",
    doc="A collected item (a signals row); the receipt version it was read from is pinned on the citation row.",
)
