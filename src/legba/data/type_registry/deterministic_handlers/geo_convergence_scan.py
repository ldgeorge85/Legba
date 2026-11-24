# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic handler ``geo_convergence_scan``.

Output: A7 geographic convergence detector — fires kind='alert' rows on the
FORMATION/DISSOLUTION edges of multi-source-family geographic convergence
(1° cells / country bins over the rolling 24h window). The returned summary
is a genuine FINDING on runs where something happened (formations /
dissolutions / first-scan seeding) and suppressed via the run's
force_trace_only on a quiet steady-state sweep (the indicator_tracker
pattern) — which keeps this handler in the FINDING-emitters set the
STRUCTURAL_VERIFY_EXEMPT drift guard asserts.

Dispatch: A7 geographic convergence detector — ~30-min LLM-free scan that
bins the rolling 24h of geolocated signals (1°×1° cells for point-
trustworthy coordinates; country bins for ISO2-tagged signals) and fires a
medium kind=alert row when ≥3 DISTINCT source families converge in one bin
(diversity is the signal). State-transition edges only (formation + one
dissolution) via trigger_class='geo_convergence' watermarks in the EXISTING
alert_trigger_watermarks table — no new migration.
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="deterministic_handler",
    id="geo_convergence_scan",
    implementation="legba.data.analysts.deterministic_handlers.geo_convergence_scan:handle",
    output="finding",
    structural_claims=True,
    doc="Geographic convergence of several source families (emission folded into the trigger scan).",
)
