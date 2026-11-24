# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic handler ``finding_supersession``.

Output: P-FS finding-level dedup / supersession — stamps supersession on
existing findings; the superseded counts live in the trace.

Dispatch: P-FS finding-level dedup / supersession (PIVOT_BUILD_PLAN §12, W3)
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="deterministic_handler",
    id="finding_supersession",
    implementation="legba.data.analysts.deterministic_handlers.finding_supersession:handle",
    output="trace_only",
    doc="Finding-level dedup: stamps supersession on existing findings.",
)
