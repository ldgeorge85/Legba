# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic handler ``event_reconciler``.

Dispatch: V3/P1 — deterministic correlated_with / evolves_from event-edge
writer.
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="deterministic_handler",
    id="event_reconciler",
    implementation="legba.data.analysts.deterministic_handlers.event_reconciler:handle",
    output="trace_only",
    doc="Writes correlated_with and evolves_from event edges.",
)
