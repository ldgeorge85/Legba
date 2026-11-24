# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic handler ``due_and_absent``.

Output: E-1 due_and_absent — what was expected by an explicit date and has
no record in this collection, reported only when its state changes (no LLM).
A run with no change is force_trace_only, not this map.

Dispatch: E-1 due_and_absent — see OUTPUT_KIND_BY_SUB_HANDLER above.

Verify exemption: E-1 — due and absent: rows of the record past an explicit
date with no matching record collected (no model, no claim about the world);
its finding cites the expectation rows.
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="deterministic_handler",
    id="due_and_absent",
    implementation="legba.data.analysts.deterministic_handlers.due_and_absent:handle",
    output="finding",
    requires_pool=True,
    alerts=("due_and_absent",),
    doc="What was expected by an explicit date and has no matching record in this collection.",
)
