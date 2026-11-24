# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic handler ``structural_balance``.

Output: Signed-edge triadic balance over the reified-Nexus graph — its
result is a structural-balance summary already captured in the trace.
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="deterministic_handler",
    id="structural_balance",
    implementation="legba.data.analysts.deterministic_handlers.structural_balance:handle",
    output="trace_only",
    doc="Signed-edge triadic balance over the entity-relationship graph.",
)
