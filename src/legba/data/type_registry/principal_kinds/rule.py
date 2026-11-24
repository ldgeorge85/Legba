# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Principal kind ``rule``."""

from legba.data.type_registry import declare


TYPE = declare(
    family="principal_kind",
    id="rule",
    name_shape="the deterministic handler id, or a named code path",
    example="rule:collection_gap",
    doc="Deterministic code: a deterministic handler (its id is its analyst id) or a named code path such as a materializer or a loader.",
)
