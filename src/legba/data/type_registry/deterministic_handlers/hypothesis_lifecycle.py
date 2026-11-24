# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic handler ``hypothesis_lifecycle``.

Output: Hypothesis lifecycle (Piece 3, Task D) — side-writes HYPOTHESIS rows
via write_hypothesis + returns a FindingPayload summary. NOT in the
operator-confirmed trace-only list, so unchanged here (left FINDING).

Dispatch: Hypothesis lifecycle (Piece 3, Task D) — emits forward-claim
hypotheses over rising situations + tests standing ones vs later evidence.
Side-writes HYPOTHESIS rows via the live write_hypothesis path; returns a
FINDING summary.
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="deterministic_handler",
    id="hypothesis_lifecycle",
    implementation="legba.data.analysts.deterministic_handlers.hypothesis_lifecycle:handle",
    output="finding",
    doc="Emits forward hypotheses over rising situations and tests standing ones.",
)
