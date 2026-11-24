# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic handler ``cross_source_dedup``.

Output: P-09 cross-source dedup (PIVOT §4.3 / P-02) — links/marks duplicate
signals; the dedup counts live in the trace.

Dispatch: P-09 cross-source dedup (PIVOT §4.3 / P-02)
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="deterministic_handler",
    id="cross_source_dedup",
    implementation="legba.data.analysts.deterministic_handlers.cross_source_dedup:handle",
    output="trace_only",
    doc="Links and marks duplicate signals across sources.",
)
