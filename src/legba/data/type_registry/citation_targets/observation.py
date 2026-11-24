# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Citation target ``observation``."""

from legba.data.type_registry import declare


TYPE = declare(
    family="citation_target",
    id="observation",
    table="observations",
    column="observation_id",
    material=True,
    ref_kinds=("observation",),
    evidence="evidence",
    doc="One number of a series (an observations row), cited by its line in the historical-series block; its receipt version is pinned.",
)
