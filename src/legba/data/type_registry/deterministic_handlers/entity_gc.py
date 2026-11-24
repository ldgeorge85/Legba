# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic handler ``entity_gc``.

Output: L-203 migrated maintenance modules — pure substrate maintenance, no
analytical finding: GC of orphaned entities, canonical entity merges,
temporal fact-decay stamps, nexus-decay stamps.
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="deterministic_handler",
    id="entity_gc",
    implementation="legba.data.analysts.deterministic_handlers.entity_gc:handle",
    output="trace_only",
    doc="Garbage-collects orphaned entities and their dangling proposed edges.",
)
