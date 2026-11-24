# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic handler ``scorecard_producer``.

Output: P4-T2 banded-scorecard producer — the REAL product = the N side-
written `kind=scorecard` rows (one banded verdict per active G20 country);
the returned summary is a per-run RECEIPT fully audited in analyst_traces,
so it belongs in the TRACE-ONLY bucket alongside structural_balance /
proposed_edge_governance. NOTE the split is intentional: TRACE_ONLY governs
ONLY the single returned summary finding — the per-country side-writes pick
their OWN kind=scorecard inside write_analyst_output and are UNAFFECTED by
this map (they are genuine persisted rows, not trace-only).

Dispatch: P4-T2 banded-scorecard producer — global sweep over active G20
countries; side-writes one kind=scorecard row per country (data.bands = the
T1 verdict, T5 eval folded), returns a TRACE_ONLY summary receipt.
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="deterministic_handler",
    id="scorecard_producer",
    implementation="legba.data.analysts.deterministic_handlers.scorecard_producer:handle",
    output="trace_only",
    doc="One banded scorecard row per active desk.",
)
