# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic handler ``graph_projector``.

Output: V3/P4b — the cross-layer graph projection: a whole-rebuild side-
write into graph_arcs (+ graph_arcs_meta receipt); the returned summary is
the per-build receipt (arc_count / source_counts / build_seconds), audited
in analyst_traces, never a finding on a trust surface.

Dispatch: V3/P4b — whole-rebuild graph_arcs projection (INSERT...SELECT per
source into graph_arcs_new, transactional rename swap). No incremental path.
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="deterministic_handler",
    id="graph_projector",
    implementation="legba.data.analysts.deterministic_handlers.graph_projector:handle",
    output="trace_only",
    doc="Whole rebuild of the cross-layer graph projection.",
)
