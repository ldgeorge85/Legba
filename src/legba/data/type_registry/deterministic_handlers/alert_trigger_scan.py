# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic handler ``alert_trigger_scan``.

Output: P1-3 verification-gated trigger scan — the REAL product = the side-
written `kind=alert` rows (one per fired verified-state transition, fanned
outward through the shared P1-1 alert-sink dispatcher); the returned summary
is a per-run counts RECEIPT fully audited in analyst_traces. TRACE_ONLY
keeps the receipt off analyst_outputs AND keeps this handler out of the
FINDING-emitters set the STRUCTURAL_VERIFY_EXEMPT_ANALYSTS drift guard
asserts equality against.

Dispatch: P1-3 verification-gated trigger scan — ~10-min sweep over verified
state TRANSITIONS (scorecard band crossings, new verified high-severity
findings, contention flips, desk baseline deviations). Side-writes
kind=alert rows + fans them through the shared P1-1 sink dispatcher; durable
watermarks (migration 0091) make every transition fire-once.
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="deterministic_handler",
    id="alert_trigger_scan",
    implementation="legba.data.analysts.deterministic_handlers.alert_trigger_scan:handle",
    output="trace_only",
    requires_pool=True,
    doc="Verification-gated trigger scan over verified state transitions.",
)
