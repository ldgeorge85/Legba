# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic handler ``profile_projector``.

Output: W9 VIEW-1 — the per-profile projection: a whole rebuild per plane
into profile_projection (+ its _meta receipt row); the returned summary is
the per-plane receipt, audited in analyst_traces, never a finding.

Dispatch: W9 VIEW-1 — whole rebuild of each profile's projection plane.
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="deterministic_handler",
    id="profile_projector",
    implementation="legba.data.analysts.deterministic_handlers.profile_projector:handle",
    output="trace_only",
    doc="Whole rebuild of each profile's projection plane.",
)
