# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic handler ``event_clustering``.

Output: V3/P1 event plane — the real products are side-written events,
links, lifecycle ledger rows and event_edges; the per-run funnel lives in
the trace. Both remain inert while the descriptor is draft / LEGBA_EVENTS
off.

Dispatch: V3/P1 — bounded event clustering over canonical signals + tower
candidates, then lifecycle maintenance through the append-only ledger.
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="deterministic_handler",
    id="event_clustering",
    implementation="legba.data.analysts.deterministic_handlers.event_clustering:handle",
    output="trace_only",
    doc="Clusters canonical signals into events and maintains their lifecycle ledger.",
)
