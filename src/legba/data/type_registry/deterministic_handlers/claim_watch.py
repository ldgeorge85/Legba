# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic handler ``claim_watch``.

Output: claim_watch — flag-only new-evidence-vs-open-question matcher. The
REAL product = the side-written append-only markers (bearing_edges +
review_flags, migration 0107); the returned summary (per-run match counts +
the staleness_debt gauge) is a RECEIPT fully audited in analyst_traces.
TRACE_ONLY (the alert_trigger_scan precedent) keeps the receipt off
analyst_outputs AND keeps this handler out of the FINDING-emitters set the
STRUCTURAL_VERIFY_EXEMPT_ANALYSTS drift guard asserts equality against.

Dispatch: claim_watch — ~30-min flag-only matcher of NEW signals (since the
durable trigger_class='claim_watch' cursor in alert_trigger_watermarks — the
0091 table ridden as a new consumer class, no new watermark table) against
the open-question set (hypotheses status='open_question'). Three fused
planes (stored signal vectors + question-embed cache / canonical entity
overlap / desk-geo overlap); above-threshold matches side-write append-only
bearing_edges + (for questions tracing FORWARD over output_consumption to
live products) review_flags. Zero LLM; never mutates any output.
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="deterministic_handler",
    id="claim_watch",
    implementation="legba.data.analysts.deterministic_handlers.claim_watch:handle",
    output="trace_only",
    requires_pool=True,
    doc="Flag-only matcher of new signals against open questions.",
)
