# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic handler ``verify_backfill``.

Output: verify_backfill (W11-F84) — the standing sweep over cited findings
whose verify leg never ran (a rolling deploy recreated the runtime mid-run):
the real product is the side-written critique; the counts live in the trace.

Dispatch: verify_backfill (W11-F84) — writes the faithfulness critique the
critic would have written for a cited finding left with none, through the
same selection + verify-and-write as rejudge_floor_critiques.py --missing.
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="deterministic_handler",
    id="verify_backfill",
    implementation="legba.data.analysts.deterministic_handlers.verify_backfill:handle",
    output="trace_only",
    doc="Writes the faithfulness critique for cited findings whose verify leg never ran.",
)
