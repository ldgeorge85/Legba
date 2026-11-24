# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic handler ``contrary_evidence_pass``.

Output: contrary_evidence_pass (7a) — the REAL product is side-written: one
claim_contentions row per material claim. The returned summary is a per-run
RECEIPT already audited in analyst_traces and mirrored into the durable
heartbeat row, and emitting it AGAIN as a kind=finding would put a retrieval
receipt into the finding stream the pass exists to contend.

Dispatch: 7a CONTRARY-EVIDENCE PASS — the only organ in the fleet that goes
looking for opposition. Daily, it takes the auditor's own material claims,
formulates ONE counter-query each (R2's polarity negation where it applies,
else one bounded core-plane call that may return a query and never a
verdict), runs it on the FREE rung, fetches the hits through the auditor's
fences, and writes a claim_contentions row carrying a stance derived in
code. No model is ever asked whether a claim is true.
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="deterministic_handler",
    id="contrary_evidence_pass",
    implementation="legba.data.analysts.deterministic_handlers.contrary_evidence_pass:handle",
    output="trace_only",
    requires_pool=True,
    doc="One counter-query per material claim; a contention row whose stance is derived in code.",
)
