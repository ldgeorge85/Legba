# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic handler ``unit_correctness_scorer``.

Output: P2-T5 per-unit correctness-vs-reference (source-id overlap recall vs
the gold labels) + faithfulness mean. A genuine measurement finding.

Dispatch: P2-T5 per-unit correctness-vs-reference scorer (deterministic,
LLM-free) — source-id overlap RECALL of each bounded unit's latest head
finding vs the operator-authored gold rows; honest-None when nothing is
scorable.
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="deterministic_handler",
    id="unit_correctness_scorer",
    implementation="legba.data.analysts.deterministic_handlers.unit_correctness_scorer:handle",
    output="finding",
    doc="Each bounded unit's latest head scored against the operator's gold rows (source-id overlap recall).",
)
