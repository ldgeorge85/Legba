# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic handler ``standing_auditor``.

Output: standing_auditor (D5) — the REAL product is side-written: one
kind=critique row per audited claim plus a kind=alert row per contradicted
high-severity claim, both through write_analyst_output with their OWN kinds
(unaffected by this map, exactly like scorecard_producer's per-country side-
writes). The returned summary is a per-run RECEIPT, fully audited in
analyst_traces and mirrored into the durable heartbeat row, so emitting it
AGAIN as a kind=finding would put an audit receipt into the finding stream
the audit is supposed to be grading.

Dispatch: D5 STANDING EXTERNAL AUDIT — the only organ in the fleet whose
truthmaker is OUTSIDE the substrate. Daily, it samples the world read + a
date-seeded rotation of desk heads, lifts 1-2 checkable world-claims from
each, checks them via the web_access PACK TOOL, and writes a SUPPORTED /
CONTRADICTED / NOT_FOUND critique per claim (its own pipeline stamp, never
the faithfulness one) plus a heartbeat row that makes the auditor itself
watchable.
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="deterministic_handler",
    id="standing_auditor",
    implementation="legba.data.analysts.deterministic_handlers.standing_auditor:handle",
    output="trace_only",
    requires_pool=True,
    doc="The standing external audit: sampled world claims checked on the web, one critique per claim.",
)
