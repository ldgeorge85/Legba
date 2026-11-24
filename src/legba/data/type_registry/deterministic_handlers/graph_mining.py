# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic handler ``graph_mining``.

Output: L-173 graph mining — communities / proxy-chains. A genuine finding.

Dispatch: L-173 original sub-handlers
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="deterministic_handler",
    id="graph_mining",
    implementation="legba.data.analysts.deterministic_handlers.graph_mining:handle",
    output="finding",
    doc="Community detection, structural-balance triads and proxy-chain mining over the knowledge graph.",
)
