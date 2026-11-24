# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic handler ``layer_divergence``.

Output: Program 6 L2 layer_divergence — per target country, the CHANGE in
that country's own layer-to-layer divergence (official vs social digest;
domestic vs foreign press; official vs public data), measured against the
country's OWN rolling baseline and fired only on a two-day move. A user-
facing analytical product: a duty officer reads "the official layer has been
running two MADs below its own fortnight for two days" the same way they
read a fired indicator. A run that fires nothing is suppressed via
force_trace_only (NOT this map — the map is the always-on kind), and its
receipt still lands in analyst_traces.

Dispatch: Program 6 L2 — the divergence-baseline unit. Pure arithmetic over
the loaded layer map (migration 0214) + the signal counts it implies: folded
per-layer daily counts, a rolling median/MAD baseline per layer pair, a
finding on a two-day |z| move, and a receipt that names WHY nothing fired on
a quiet day. No LLM, no new table — the series rides analyst_outputs.data
(the indicator_tracker read).

Verify exemption: Program 6 L2: the layer-divergence unit is an INSTRUMENT
over counts. Its finding asserts arithmetic — this layer carried N folded
dispatches on this day, that ratio sits Z MAD-scaled deviations from its own
fortnight — and every sentence of its body is generated FROM those numbers,
so there is no model prose in the row for a faithfulness judge to grade. It
does carry re-derivable identities, which is why it also joins
STRUCTURAL_CLAIMS_VERIFY_ANALYSTS below: the honest badge and a REAL re-
derivation, rather than the badge alone. Added here because
OUTPUT_KIND_BY_SUB_HANDLER marks it FINDING and this set MUST stay equal to
the FINDING emitters.

Structural claims: Program 6 L2 — the layer-divergence unit asserts two
identities that are re-derivable from its own payload with no DB access: the
divergence partition (total = widening + narrowing, true by construction of
`evaluate_pair`'s single direction label) and the distinct-desk count over
its per-desk receipts. A partition bug in either would surface as a flagged
critique rather than as a number nobody checked.
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="deterministic_handler",
    id="layer_divergence",
    implementation="legba.data.analysts.deterministic_handlers.layer_divergence:handle",
    output="finding",
    structural_claims=True,
    doc="A country's layer-to-layer divergence against its own rolling baseline.",
)
