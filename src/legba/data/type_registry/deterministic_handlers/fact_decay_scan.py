# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic handler ``fact_decay_scan``.

Output: C4 fact confidence-decay readout stamper — daily scan that stamps
the fact_decay_states SIDECAR (migration 0098; NEVER mutates a facts row,
unlike the legacy fact_decay sweep) and emits the honest per-state
distribution summary (counts per fresh/aging/stale/revoke_candidate + top
revoke candidates; zero-state honest). The distribution IS the measurement
product (the calibration_tracking precedent), so it stays a genuine FINDING
— which keeps this handler in the FINDING-emitters set the
STRUCTURAL_VERIFY_EXEMPT drift guard asserts.

Dispatch: C4 fact confidence-decay readout stamper — daily walk of every
OPEN fact computing the derived MISP-curve readout (legba.data.facts.decay:
per-class lifetimes, sightings derived from the corroboration-unioned
derived_from signal ids, reaction points + revoke threshold) into the
fact_decay_states sidecar (0098). Readout only: NEVER touches facts.
Consumption ships OFF behind LEGBA_FACT_DECAY_WEIGHTING.
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="deterministic_handler",
    id="fact_decay_scan",
    implementation="legba.data.analysts.deterministic_handlers.fact_decay_scan:handle",
    output="finding",
    requires_pool=True,
    doc="Daily decay readout stamped into the fact decay sidecar; never touches facts.",
)
