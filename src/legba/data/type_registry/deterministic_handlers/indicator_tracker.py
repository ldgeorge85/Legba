# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic handler ``indicator_tracker``.

Output: S3-T2 indicator_tracker — diffs the structured I&W indicators run-
over-run per unit-stream + emits a summary FINDING on status FLIPS (esp.
not_observed→triggered). A user-facing analytical product (the fired warning
signposts a duty officer reads); a no-flip sweep is suppressed via the run's
force_trace_only (NOT this map — the map is the always-on kind).

Dispatch: S3-T2 indicator_tracker — deterministic run-over-run diff of the
structured I&W indicators per (target_id, source unit analyst_id); emits a
summary finding on status flips, trace-only on a no-flip/unchanged sweep.
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="deterministic_handler",
    id="indicator_tracker",
    implementation="legba.data.analysts.deterministic_handlers.indicator_tracker:handle",
    output="finding",
    structural_claims=True,
    doc="Run-over-run flips of the structured indicators, debounced.",
)
