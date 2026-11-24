# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic handler ``thematic_proposal``.

Output: thematic_proposal (5b) — PROPOSES thematic frames for uncovered hot
situations. The proposal finding is a user-facing, actionable product (an
operator reads it + registers the suggested target), so it is a real
FINDING, NOT trace-only.

Dispatch: Thematic proposal (5b) — proposes thematic frames for uncovered
hot situations (detect → propose → operator-promote).
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="deterministic_handler",
    id="thematic_proposal",
    implementation="legba.data.analysts.deterministic_handlers.thematic_proposal:handle",
    output="finding",
    structural_claims=True,
    doc="Proposes thematic frames for uncovered hot situations.",
)
