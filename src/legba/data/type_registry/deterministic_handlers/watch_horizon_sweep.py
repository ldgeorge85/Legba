# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic handler ``watch_horizon_sweep``.

Output: watch_horizon_sweep (W5) — closes OPEN consult watches whose marker
horizon has passed and the folded duplicates; the real product is the side-
written status change on `hypotheses` rows, the returned summary (counts +
the closed ids) is a RECEIPT audited in analyst_traces. TRACE_ONLY (the
claim_watch precedent).

Dispatch: watch_horizon_sweep (W5) — closes consult watches at their horizon
(status 'expired') and folded duplicates (status 'withdrawn'); sibling to
claim_watch on the same ~30-min cadence. Zero LLM.
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="deterministic_handler",
    id="watch_horizon_sweep",
    implementation="legba.data.analysts.deterministic_handlers.watch_horizon_sweep:handle",
    output="trace_only",
    doc="Closes consult watches at their horizon and folded duplicates.",
)
