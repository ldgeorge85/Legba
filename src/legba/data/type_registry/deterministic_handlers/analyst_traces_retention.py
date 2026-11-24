# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic handler ``analyst_traces_retention``.

Output: S-6 analyst_traces TTL purge — mirrors signals_retention exactly: an
honest per-run summary FINDING (traces/critiques counts; zero-state honest),
disabled by default (ttl_days<=0). Keeps this handler in the FINDING-
emitters set the STRUCTURAL_VERIFY_EXEMPT drift guard asserts. C2 "one
janitor": also now delegates to _retention_sweep (the
"analyst_traces_retention" policy row) — same unchanged name/kind.

Dispatch: S-6 analyst_traces TTL purge — bounds the unbounded
debug/telemetry table (~470MB/164k rows, +5.4k/day; disk-creep source).
Disabled by default (ttl_days<=0); FK children DB-handled (critiques
CASCADE, DLQ run_id SET NULL). TTL must stay above the 7-day cadence-health
window. C2 "one janitor": delegates to the same shared _retention_sweep
engine as signals_retention (both execute a retention_policies config row).
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="deterministic_handler",
    id="analyst_traces_retention",
    implementation="legba.data.analysts.deterministic_handlers.analyst_traces_retention:handle",
    output="finding",
    doc="TTL purge of aged analyst traces (off by default).",
)
