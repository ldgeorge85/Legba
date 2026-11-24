# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic handler ``proposed_edge_governance``.

Output: Proposed-edge governance (FIX P3-1) — promotes corroborated
co_occurs proposed_edges into nexuses (via the live write_nexus side-write)
+ ages out thin stale ones; the promotion/aging counts live in the trace.

Dispatch: Proposed-edge governance (FIX P3-1) — promotes corroborated
co_occurs proposed_edges to nexuses + flips status; ages out thin stale
ones.
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="deterministic_handler",
    id="proposed_edge_governance",
    implementation="legba.data.analysts.deterministic_handlers.proposed_edge_governance:handle",
    output="trace_only",
    doc="Promotes corroborated proposed edges and ages out thin stale ones.",
)
