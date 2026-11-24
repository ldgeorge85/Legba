# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic handler ``source_track_record``.

Output: A6 P3-3 source track record — daily recompute of the per-source
EARNED record (wins/losses over RESOLVED contentions + corroboration
outcomes; smoothed win-rate) into source_track_records (migration 0099). The
honest per-source distribution IS the measurement product (the calibration_
tracking / fact_decay_scan precedent), so it stays a genuine FINDING — which
keeps this handler in the FINDING-emitters set the STRUCTURAL_ VERIFY_EXEMPT
drift guard asserts. Grades feed weighting/tie-break/display ONLY, never
faithfulness (A6 hard rule).

Dispatch: A6 P3-3 EARNED source track record — daily recompute of per-source
wins/losses over RESOLVED fact_contention groups (+ corroboration) with a
Beta-smoothed win-rate, stored in source_track_records (0099). The arbiter
consumption seam (_earned_track_record_weight) is OFF behind
LEGBA_CONTENTION_EARNED_WEIGHT; circularity-guarded (lag + live exclusion).
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="deterministic_handler",
    id="source_track_record",
    implementation="legba.data.analysts.deterministic_handlers.source_track_record:handle",
    output="finding",
    requires_pool=True,
    doc="Daily per-source earned record over resolved contentions.",
)
