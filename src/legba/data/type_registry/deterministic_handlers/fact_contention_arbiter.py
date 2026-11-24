# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic handler ``fact_contention_arbiter``.

Output: Holes-B contested-claims arbiter (#101) — DETECT-ONLY. Its real
product is side-written (the fact_contention* sidecar + the facts marker
columns); the per-run counts (groups open / abstained / junk-excluded) live
in the trace.

Dispatch: Holes-B contested-claims arbiter (#101, DETECT-ONLY) — builds the
fact_contention* sidecar + stamps the facts markers; never closes a fact.
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="deterministic_handler",
    id="fact_contention_arbiter",
    implementation="legba.data.analysts.deterministic_handlers.fact_contention_arbiter:handle",
    output="trace_only",
    doc="Detect-only contested-claims arbiter: builds the contention sidecar and marks facts.",
)
