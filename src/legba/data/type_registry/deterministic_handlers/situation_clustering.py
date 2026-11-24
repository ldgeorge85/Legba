# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic handler ``situation_clustering``.

Output: Situation clustering — materializes the `situations` table from the
signatures supersession stamps; the run summary is a FindingPayload (the
situation rows are side-written directly to the situations table). KEEP per
the operator-confirmed split (verified: situation_clustering's OUTPUT_KIND
stays FINDING — its summary IS a substantive product).

Dispatch: Situation clustering — materializes `situations` from stamped
signatures.
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="deterministic_handler",
    id="situation_clustering",
    implementation="legba.data.analysts.deterministic_handlers.situation_clustering:handle",
    output="finding",
    doc="Materialises situations from stamped signatures.",
)
