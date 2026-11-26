# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic handler ``correctness_grader``.

Output: G1 correctness_grader — THE PER-UNIT CORRECTNESS INSTRUMENT. A
genuine FINDING on the desk_reference precedent: a counting-not-repairing
receipt carrying data.per_target (each unit's correctness_share BESIDE its
coverage_share, both None-not-0.0 on an empty denominator) plus the
calibration row it ran under and the ceiling in force. The NUMBERS
themselves are side-written to unit_correctness / unit_correctness_claims
(migration 0196); this receipt is the sweep's own watchable heartbeat, and
it NEVER alerts and NEVER gates — the composition gate is track G2.

Dispatch: G1 correctness_grader — PROGRAM 1'S INSTRUMENT, AS A JOB. Per
(country target with a live unit_references row, as-of stamp): freeze the
desk + composition heads, segment with the SHIPPED segmenter, build ONE
byte-identical leak-scanned packet against the reference, refuse unless a
passing grader_calibrations row covers this rubric sha and these model ids,
grade with F0 ($0 core plane) on every claim and F2/F3 only on the claims F0
did not call `silent` and only while LEGBA_GRADER_DAILY_CEILING_USD allows
(default $0 = never), adjudicate >=2 of 3 else `split`, and write the unit
row + its per-claim ledger. Flag LEGBA_CORRECTNESS_GRADER_ENABLED, default
OFF: off, it writes nothing at all.

Verify exemption: the
correctness grader is an INSTRUMENT. Its receipt reports what the instrument
did — which units it graded, against which reference, under which
calibration, at what cost — and asserts nothing about the world that a
faithfulness judge could grade. Routing it into the verify population would
also be circular in the worst way: the pass that measures groundedness would
be scoring the output of the pass that exists because groundedness is not
truth. Added here because OUTPUT_KIND_BY_SUB_HANDLER marks it FINDING and
this set MUST stay equal to the FINDING emitters.
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="deterministic_handler",
    id="correctness_grader",
    implementation="legba.data.analysts.deterministic_handlers.correctness_grader:handle",
    output="finding",
    requires_pool=True,
    doc="Grades each unit's claims against its reference, under a passing calibration.",
)
