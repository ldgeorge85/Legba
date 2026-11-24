# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic handler ``narrative_mapper``.

Output: P4-1/P4-2 narrative mapper — reifies contested-claim families as
`narratives` rows (carrier sources + first-seen/echo lags + propagation
edges) + refreshes the directed source-echo graph (`narrative_echo_edges`),
both DERIVED and wholesale-refreshed from the contention sidecar (mig 0102).
DETECT-ONLY (never mutates facts); echo-lead is descriptive co-carriage
timing, not causal. The honest per-run distribution IS the measurement
product (the source_track_record / fact_decay_scan precedent), so it stays a
genuine FINDING — which keeps this handler in the FINDING-emitters set the
STRUCTURAL_VERIFY_EXEMPT drift guard asserts.

Dispatch: P4-1/P4-2 narrative mapper — DAILY deterministic META sweep that
reifies every active contested-claim family (`fact_contention` group) into a
`narratives` row with its carrier sources, per-source first-seen, echo lags
and propagation ordering, and refreshes the directed source-echo graph
(`narrative_echo_edges`: leader->follower co-carriage + lag over the
narrative population). Reads the contention sidecar + fact->signal->source
lineage; wholesale-refreshes both derived tables (mig 0102). DETECT-ONLY.

Structural claims: narrative_mapper landed in
STRUCTURAL_VERIFY_EXEMPT_ANALYSTS (P4-1 wave) and emits a re-derivable
rollup identity (narratives_total = contested + surfaced, always true by
REIFIED_STATUSES construction) — so it joins the claims-verified set per the
C2b merge note. Subset drift guard holds.
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="deterministic_handler",
    id="narrative_mapper",
    implementation="legba.data.analysts.deterministic_handlers.narrative_mapper:handle",
    output="finding",
    requires_pool=True,
    structural_claims=True,
    doc="Reifies contested-claim families as narratives and refreshes the source-echo graph.",
)
