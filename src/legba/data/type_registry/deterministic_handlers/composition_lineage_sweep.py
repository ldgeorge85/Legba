# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic handler ``composition_lineage_sweep``.

Output: P3-T6 composition lineage-integrity sweep — the crown's per-floor
derived_from BFS over the composition roots (0 dangling/cycles on a healthy
tower; NAMES a broken sub-claim floor). A substantive verification product.

Dispatch: P3-T6 composition lineage-integrity sweep — per-floor derived_from
BFS (validate_lineage) over the world/country composition roots. Refuses
loud.
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="deterministic_handler",
    id="composition_lineage_sweep",
    implementation="legba.data.analysts.deterministic_handlers.composition_lineage_sweep:handle",
    output="finding",
    requires_pool=True,
    doc="Per-floor derived_from walk over the composition roots; names a broken floor.",
)
