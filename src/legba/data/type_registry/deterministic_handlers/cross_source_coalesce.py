# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic handler ``cross_source_coalesce``.

Output: P2 cross-source semantic/temporal coalesce — substrate-wide near-dup
linker; the coalesce counts live in the trace.

Dispatch: P2 cross-source semantic/temporal coalesce (review data-integrity)
— the substrate-wide near-dup linker (reuses Dedupe4TierHandler tier-3/4).
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="deterministic_handler",
    id="cross_source_coalesce",
    implementation="legba.data.analysts.deterministic_handlers.cross_source_coalesce:handle",
    output="trace_only",
    doc="Substrate-wide semantic and temporal near-duplicate linker.",
)
