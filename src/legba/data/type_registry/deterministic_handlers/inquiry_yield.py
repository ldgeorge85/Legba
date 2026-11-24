# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic handler ``inquiry_yield``.

Output: H12 inquiry_yield — Program 5 lane 1's weekly ledger instrument
(PROGRAM5_INQUIRY_DESIGN_2026-09-24.md §4). The per-descriptor counters ARE
the product — there is no side table this receipt summarizes (unlike
receipt_anchor / forecast_scoreboard, which are TRACE_ONLY because their
real product is a dedicated ledger row) — so it stays a genuine FINDING, on
the calibration_tracking / research_measurement precedent. It is also
required here: STRUCTURAL_VERIFY_EXEMPT_ANALYSTS (kinds.py) must equal the
FINDING-emitting deterministic sub-handlers, and a pure-arithmetic weekly
count has no model prose for the faithfulness verify pass to grade.

Dispatch: H12 inquiry_yield — Program 5 lane 1's weekly instrument over the
inquiry_ledger (migration 0215): per descriptor, hypotheses
opened/confirmed/refuted/expired, questions dispatched/answered,
observations a desk later carried forward ("anticipated" — its close-time
cited_refs overlap a LATER finding's derived_from), and blind spots (a
dispatched, still-open question whose target had no read this cycle). Pure
arithmetic over already-materialized rows; no LLM. Published as a genuine
FINDING (never TRACE_ONLY — the receipt IS the product, there is no side
table it summarizes) and never a gate.

Verify exemption: H12 (Program 5 lane 1,
PROGRAM5_INQUIRY_DESIGN_2026-09-24.md §4): inquiry_yield is a deterministic
weekly COUNT over the inquiry_ledger — hypotheses
opened/confirmed/refuted/expired, questions dispatched/ answered,
observations later carried forward, blind spots — no model prose in the row
to grade. Added here because OUTPUT_KIND_BY_SUB_HANDLER marks it FINDING and
this set MUST stay equal to the FINDING emitters.
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="deterministic_handler",
    id="inquiry_yield",
    implementation="legba.data.analysts.deterministic_handlers.inquiry_yield:handle",
    output="finding",
    doc="Weekly counts over the inquiry ledger, per descriptor.",
)
