# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic handler ``reenrich_pending``.

Output: reenrich_pending (W11-F118) — the standing sweep over rows the
source actor wrote UNENRICHED (payload.enrichment_pending): re-runs the
row's own source chain and un-marks it. Side-written enrichment; the counts
live in the trace.

Dispatch: reenrich_pending (W11-F118) — re-enriches the rows written with
payload.enrichment_pending through the same chain the ingest path runs
(run_baseline + the source's own pipeline.enrichment stage), oldest first,
bounded per run; the marker is removed on success and kept on failure.
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="deterministic_handler",
    id="reenrich_pending",
    implementation="legba.data.analysts.deterministic_handlers.reenrich_pending:handle",
    output="trace_only",
    doc="Re-enriches rows the source actor wrote unenriched.",
)
