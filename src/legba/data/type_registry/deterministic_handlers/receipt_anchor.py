# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic handler ``receipt_anchor``.

Output: H13 receipt_anchor — the REAL product is the side-written
`receipt_anchors` rows (day × calendar Merkle-root OpenTimestamps
attestations); the returned summary is a per-run counts receipt. TRACE_ONLY
so the anchor is never a finding on a trust surface.

Dispatch: H13 receipt_anchor — daily external timestamp on the receipt
chain: Merkle root over the latest analyst_traces.receipt_hash per analyst,
POSTed to the OpenTimestamps public calendars; side-writes one
`receipt_anchors` row per (day, calendar) ('submitted'/'pending', retried
next tick). Reads + writes via deps.pg_pool.
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="deterministic_handler",
    id="receipt_anchor",
    implementation="legba.data.analysts.deterministic_handlers.receipt_anchor:handle",
    output="trace_only",
    doc="Daily external timestamp (OpenTimestamps) on the receipt chain.",
)
