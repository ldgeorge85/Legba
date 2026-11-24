# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic handler ``desk_reference``.

Output: A-1 ATTENTION MEASUREMENT — the out-of-plane daily desk reference
plus the two diffs it feeds (collection_recall / attention_rate) and the
validity harness that grades them. The RETURNED summary IS the measurement
product (the calibration_tracking / fact_decay_scan /
composition_lineage_sweep precedent): a counting-not-repairing GAUGE
carrying data.collection_gauge / data.attention_gauge / data.instrument,
every ratio None-not-0.0 on an empty denominator. So it stays a genuine
FINDING — which keeps this handler in the FINDING-emitters set the
STRUCTURAL_VERIFY_EXEMPT_ANALYSTS drift guard asserts equality against. It
NEVER alerts and NEVER dispatches: A-4 (the reference_gap research side-
write) and A-5 (the attention_gap trigger class) are separate trains, and
A-5 is gated on the harness.

Dispatch: A-1 desk_reference — THE OUT-OF-PLANE DAILY DESK REFERENCE. Daily
META sweep: per (read-set target x bounded unit) it runs ONE code-built
web_search through the web_access pack, hands a THIRD-family model (the
descriptor binds llm.judge.cerebras_gemma4_31b — not the writer's family,
not the judge's) the unit's own method.bounded_question plus those results
and NO substrate row, and writes the answer to unit_reference_labels (mig
0057 + 0191). Then it runs both diffs ($0, no LLM) and publishes the gauge.
Flag LEGBA_DESK_REFERENCE_ENABLED, default OFF: off, the handler writes
nothing at all.

Verify exemption: A-1 (ATTENTION_MEASUREMENT_DESIGN §3.1) — the attention
instrument is an INSTRUMENT, not a claim about the world, and it must never
enter the verify/judge population: this design would otherwise itself add to
the ~84% of LLM calls that are the system watching itself. It is also
required here by the drift guard, which asserts this set EQUALS the FINDING-
emitting deterministic sub-handlers.
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="deterministic_handler",
    id="desk_reference",
    implementation="legba.data.analysts.deterministic_handlers.desk_reference:handle",
    output="finding",
    requires_pool=True,
    doc="The out-of-plane daily desk reference and the two attention diffs it feeds.",
)
